# Design: SenseVoice-Small STT 修通 + fsmn-vad 替换

Date: 2026-08-25

## Goal

把已安装的 SenseVoice-Small（funasr 1.4.2，本地模型，GPU）真正接入现有 ASR 流程：修复 `SpeechRecognizer` 在 funasr 1.4.2 下必然崩溃的调用，使模型可配置（本地路径/设备/语言），并把 `SpeechBuffer` 的 webrtcvad 逐帧判定替换为 funasr fsmn-vad 周期切分。改动收敛在 cognition 侧，不改 ASR 状态机与总线协议。

## 现状判断

ASR 全链路骨架（2026-08-23 设计）已落地：`WakeWordDetector` → `Topics.AWAKE` → `AsrSession` 状态机 → `SpeechBuffer`(webrtcvad) → `SpeechRecognizer`(SenseVoice) → `Topics.USER_UTTERANCE` → `DecisionHub.on_user_utterance`。

实测发现三个问题：

1. **`SpeechRecognizer._infer()` 必然崩溃**（`stt.py:49`）：调 `model(input=samples, fs=sample_rate)`，但 funasr 1.4.2 的 `AutoModel` 需走 `generate(input=..., cache={}, language=..., use_itn=True)`。直接调用触发 `TypeError: SenseVoiceSmall.forward() missing 3 required positional arguments`。
2. **无配置**：`build_pipeline` 恒用默认 `SpeechRecognizer()`（`model="iic/SenseVoiceSmall"`、device 默认 CPU、无本地路径）。CPU 加载实测 ~70s；CUDA 加载 ~3s、RTF≈0.01。用户已装本地模型（`D:\modelscope\models\iic--SenseVoiceSmall\snapshots\master`）。
3. **VAD 粗糙**：`SpeechBuffer` 用 webrtcvad（aggressive=0）逐 20ms 帧判定，误判率高，边界不准。

## 关键事实：funasr 1.4.2 的 fsmn-vad 流式不可用

实测 `AutoModel(model="fsmn-vad").generate(input=chunk, cache=..., is_final=False, chunk_size=200)`：
- `AutoModel.inference()` 在调用模型前 `kwargs.pop("cache")`（`auto_model.py` 第 31-32 行），流式状态被丢弃；
- 结果中段 `value` 永远 `[start, -1]`（结束点不闭合），`is_final` 空输入也返回空。

因此**不做 fsmn-vad 流式接入**。改用其**非流式切分**（`generate(input=整段)` 返回 `value=[[start_ms,end_ms],...]`），已实测可靠。边界判定延迟由一个 `vad_interval_ms` 周期 + funasr 自身 end-silence 构成，默认约 1.2s，换来神经 VAD 的边界准确率；若需更低延迟可调参。

## 设计

### 1. Config：新增 `SttConfig`

`src/yuki/config.py`，镜像 `LocalBrainConfig`/`VlmConfig` 写法，并挂到 `Config.stt` 与 env 映射表：

```python
class SttVadConfig(BaseModel):
    model: str = "fsmn-vad"
    vad_interval_ms: int = Field(400, ge=50, le=5000)
    end_silence_ms: int = Field(800, ge=100, le=10000)
    max_utterance_s: float = Field(10.0, ge=1.0, le=60.0)


class SttConfig(BaseModel):
    enabled: bool = True
    model: str = "iic/SenseVoiceSmall"   # hub 名；model_dir 为空时用
    model_dir: str = ""                  # 本地模型目录覆盖
    device: str = "auto"                 # auto | cpu | cuda:0 ...
    language: str = "auto"               # auto/zn/en/yue/ja/ko/nospeech
    use_itn: bool = True                 # 数字/标点规整
    warmup: bool = True                  # 启动时后台预热
    retry_window_s: float = Field(60.0, ge=0.0)
    vad: SttVadConfig = Field(default_factory=SttVadConfig)
```

`config.example.yaml` 增补 `stt:` 段；`Config.load` 的 env 映射列表加入 `("stt", SttConfig)`（嵌套 `vad` 走 `YUKI_STT_VAD_*`，由 `_apply_env` 对嵌套模型字段递归调用，参考现有实现扩展）。

### 2. `SpeechRecognizer`（`cognition/stt.py`）修复

- 构造参数新增：`model_id="iic/SenseVoiceSmall"`、`model_dir=""`、`device="auto"`、`language="auto"`、`use_itn=True`（保留 `model=` 注入实例，兼容测试）。
- `_resolve_device()`：`auto` → `torch.cuda.is_available()` ? `"cuda:0"` : `"cpu"`；否则按给定值。
- `_load()`：`AutoModel(model=self._model_dir or self._model_id, device=self._resolve_device(), disable_update=True, trust_remote_code=True)`；加 `threading.Lock()`（`_load_lock`，仿 `VisualUnderstander`）防 warmup 与首帧识别竞态。
- `_infer()` 改走 generate API + 富后处理（funasr 懒导入，保持模块级无 funasr 依赖）：

```python
from funasr import AutoModel  # 在 _load 内
from funasr.utils.postprocess_utils import rich_transcription_postprocess  # 在 _infer 内
result = self._model.generate(
    input=samples.astype(np.float32), fs=sample_rate,
    cache={}, language=self._language, use_itn=self._use_itn,
)
return rich_transcription_postprocess(str(result[0]["text"]))
```

- 新增 `warmup()`（仿 VLM）：`if loaded or not gate.can_load(): return`；后台 daemon 线程调 `_load()`，失败仅 `logger.warning` 降级。
- `health()` 增补 `device`、`model`、`model_dir` 字段。

### 3. VAD 替换：`SpeechBuffer` + `FsmnVadBackend`

**`FsmnVadBackend`**（新文件 `cognition/vad.py`，或并入 `speech_buffer.py`——随实现定，倾向独立文件便于测试）：

- 构造：`model="fsmn-vad"`、`device`（复用 stt 的解析逻辑）、`sample_rate=16000`。
- 懒加载 `AutoModel(model=self._model, device=..., disable_update=True, trust_remote_code=True)`；`threading.Lock()` 保护。
- 接口 `segments(samples: np.ndarray) -> list[list[int]]`：`self._model.generate(input=samples)` → `value`（`[[start_ms, end_ms], ...]`，ms）。空/异常返回 `[]`。
- `warmup()`/`health()` 与 `SpeechRecognizer` 对齐。

**`SpeechBuffer` 重构**（接口不变：`add_frame`/`reset`/`on_utterance`/`has_speech`）：

- 参数改为：`vad`（可注入 fake，默认 `FsmnVadBackend`）、`frame_ms=20`、`sample_rate=16000`、`vad_interval_ms=400`、`end_silence_ms=800`、`max_utterance_s=10`、`on_utterance`。
- 内部逻辑：
  - `add_frame(samples)`：累积 float32 帧到 `_pending`（并按旧语义在语音段期间把帧追加进 `_speech`，保证 `has_speech()` 兼容 `AsrSession`）。
  - 每凑满 `vad_interval_ms` 的音频，调 `vad.segments(self._audio)` 得段列表 `segs`（ms）。
  - 状态判定（用 `_audio` 总时长 `T_ms`）：
    - `segs` 为空且 `_speech` 为空 → 继续监听（无语音，不 flush）。
    - 尾静音判定：`last_end = segs[-1][1] if segs else 0`；若 `T_ms - last_end >= end_silence_ms` 且曾有过语音 → flush。
    - 若 `len(self._audio) >= max_utterance_s` → flush（防止无限累积）。
  - `flush()`：拼接 `_speech` 调 `on_utterance`；清空 `_audio`/`_speech`/`_pending`。无 `on_utterance` 或空语音则只清空。
  - `reset()`：清空全部缓冲，丢弃 VAD 状态（fsmn-vad 无跨段状态，无需额外重置）。
- 累积上限：`_audio` 超 `max_utterance_s` 时**先 flush 再截断**，防止无界增长。

**取舍记录**：保留 `webrtcvad-wheels` 依赖（避免安装扰动），重构后 `SpeechBuffer` 不再使用它；后续可择机移除。

### 4. assembly 接线（`cognition/assembly.py`）

- `assemble()` 构造配置化实例：
  - `stt = SpeechRecognizer(enabled=cfg.stt.enabled, model_id=cfg.stt.model, model_dir=cfg.stt.model_dir, device=cfg.stt.device, language=cfg.stt.language, use_itn=cfg.stt.use_itn, retry_window_s=cfg.stt.retry_window_s)`
  - `speech_buffer = SpeechBuffer(vad=FsmnVadBackend(model=cfg.stt.vad.model, device=cfg.stt.device), vad_interval_ms=cfg.stt.vad.vad_interval_ms, end_silence_ms=cfg.stt.vad.end_silence_ms, max_utterance_s=cfg.stt.vad.max_utterance_s)`
  - 传入 `build_pipeline(stt=stt, speech_buffer=speech_buffer, ...)`。
- `PerceptionPipeline` 新增 `warmup_stt()`（代理 `self._stt.warmup()`，容缺）；assemble 里 `pipeline.warmup_stt()` 与 `warmup_vlm()` 并列调用。
- 注意：`build_pipeline`/`PerceptionPipeline` 默认 `speech_buffer=None` 时 `AsrSession` 内部新建的 `SpeechBuffer` 也要用 fsmn-vad 默认后端（不再是 webrtcvad）。

### 5. pyproject

`asr` extra 增加 `"funasr"`（`asr = ["openwakeword", "onnxruntime", "funasr"]`）。

## 错误处理

- **STT 加载失败**：`LoadGate` 现有重试窗口语义不变；`recognize()` 返回空字符串，不上报。warmup 失败仅告警。
- **VAD 后端失败/异常**：`FsmnVadBackend.segments` 内部捕获返回 `[]`；`SpeechBuffer` 对异常帧跳过，不因 VAD 崩溃整条 ASR。
- **GPU 不可用**：`device="auto"` 回退 CPU（延迟上升，流程不断）。
- **fsmn-vad 无段**：`segs` 为空 → 持续监听；长时间无语音由 `AsrSession` 唤醒超时回 IDLE。

## 测试

- `tests/cognition/test_stt.py`（改造 + 新增）：
  - FakeModel 改 `generate(**kwargs)` 形态；断言 `fs`/`input` dtype/`cache`/`language`/`use_itn` 透传。
  - `device="auto"` 解析（monkeypatch torch.cuda.is_available）；`warmup()` 后台加载与失败降级；`rich_transcription_postprocess` 被调用。
- `tests/cognition/test_speech_buffer.py`（重写）：fake `vad.segments()` 后端覆盖——语音→尾静音 flush、max 时长 flush、长时间无语音不 flush、reset 清空、`has_speech` 语义。
- `tests/cognition/test_assembly.py`（增补）：断言 build_pipeline 收到配置化 stt/vad 参数、`warmup_stt` 被调用。
- config 解析测试：`stt` 段 + env 覆盖。
- `@pytest.mark.e2e`：真实本地模型对 `example/zh.mp3`/`en.mp3` 识别并断言非空（跳默认 CI）。

## 验证

- `pytest` 全量绿。
- e2e 冒烟：真实麦克风说一句话 → 触发识别 → 文字发布；静默 → 状态回落 IDLE。
- 手工：`config.yaml` 设 `stt.model_dir` 指向本地目录 + `device: auto`，`python -m yuki.cognition` 启动日志无异常，health 显示 `stt.loaded=true`。

## Out of Scope

- 不做 fsmn-vad 流式接入（funasr 1.4.2 AutoModel 丢弃 cache，不可用）。
- 不重做 ASR 状态机/总线协议/唤醒词语义。
- 不更换 webrtcvad-wheels 依赖（保留但不再使用）。
- 不处理 TTS 输出侧（interaction 仍是打印桩）。
