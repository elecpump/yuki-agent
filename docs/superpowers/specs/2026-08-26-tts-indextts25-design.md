# Design: IndexTTS 2.5 接入 interaction 进程（真实 TTS 输出）

Date: 2026-08-26

## Goal

将 IndexTTS 2.5 嵌入 interaction 进程，替换当前 stub TTS（`InteractionAgent.TTS`，控制台打印 `[yuki] {text}`），实现真实语音输出：emotion 驱动的流式合成 + pyaudio 分段流式播放（segment 级，合成完一段播一段），并在模型不可用时降级回控制台打印（保持 e2e 断言不变）。

**核心决策**：

- IndexTTS 2.5 **嵌入 interaction 进程**（非独立进程），进程内直接调用 SDK。
- pyaudio OutputStream **流式播放**（SDK 输出 22050 Hz；流式粒度为 **segment 级**（已确认），每段合成完返回一个 torch.Tensor，归一化为 16-bit mono PCM 后"合成完一段播一段"）。
- emotion 为**可选增强**：REPLY 可不携带 emotion；不指定/`neutral` 时 `emo_vector=None`，**沿用参考音频的自然情绪**（不构造"neutral 向量"）。
- 降级策略：IndexTTS 不可用 → 控制台打印（`[yuki] {text}` 前缀不变，e2e 依赖此格式）。
- **线程模型**：`speak()` 仅执行轻量控制操作（递增 generation、必要时立即停止当前播放、覆盖待处理 job）后返回；合成 + 播放跑在 TtsController 专用 worker 线程，绝不在 bus 订阅回调线程里执行（GPU 合成 + 阻塞播放会卡死订阅分发）。
- **语言策略**：合成必须显式传 `lang`（SDK `infer(..., lang, ...)` 无默认）；本期由 `TtsConfig.language` 配置决定（默认 `zh`），语言自动检测留后续。
- **参考音频为硬依赖**：SDK 直接读取 `spk_audio_prompt`，**无默认音色**；配置校验失败（文件缺失/不可读）→ 禁用模型 → console 降级，不存在"引擎默认声音"。

## 现状判断

- `InteractionAgent.TTS` 桩：`agent.py:16-21`，`speak(text)` 打印 `[yuki] {text}`；`health_components()` 硬编码 `{"output": "console"}`（`agent.py:112`）。
- `LoadGate`（`cognition/load_gate.py`）三态加载门已成熟，VLM/STT/VAD 均复用（`vlm.py:52` 等）；`VisualUnderstander` 的 `warmup()` 后台线程模式（`vlm.py:100-108`）是 IndexTTSModel 的直接模板。
- **emotion 链路当前是断的**：DecisionHub 计算了 `result["emotion"]`（`brain/hub.py:277` 等），但发布 REPLY 时只发 `{"text", "ts"}`（`hub.py:241`），`handle_awake_request` 响应同样不带 emotion（`hub.py:286`）；`ReplyPayload`（`payloads.py:10-13`）无 emotion 字段。→ 本设计必须显式改造此链路（§4）。
- **Emotion 枚举实际只有 7 个值**（`brain/classifier.py:17-25`）：`neutral / joy / sadness / anxiety / anger / love / tired`（非文档早期草稿中的 9 种）。→ 映射表见 §5.4。
- `AsrSession`（`cognition/asr_session.py`）状态机 `idle/listening/speaking/processing` 中 `speaking` 表示**用户正在说话**（VAD 检出，`asr_session.py:90`），不是 TTS 播放；且该类纯逻辑、**不依赖 bus/topic**（`asr_session.py:14`），订阅事件须由拥有它的 `PerceptionPipeline`（`pipeline.py:148`）完成。→ 状态/订阅归属见 §6。
- `Topics.TTS_REF = "audio/tts_ref"` 已存在（`topics.py:10`），原始架构语义为"交互层把 TTS 播放音频发布到该主题，采集层作为 **AEC 回声消除远端参考**"（`2026-08-10-yuki-agent-design.md` §7）。与"克隆音色的参考音频文件"（`reference_audio_path`）是**两个概念**，必须区分（§5.3 注）。
- `Config` 为 `extra="forbid"` + `load()` 显式 section 列表（`config.py:197-258`）：新增配置段需三处接入（§7）。
- `pyproject.toml` 无 indextts/pyaudio；`ml` extra 含 torch/transformers。**indextts 未发布到 PyPI**，官方安装为 git clone + `uv sync` → 依赖方案见 §8（锁定 commit 的 Git 依赖 / vendor）。
- **已核实的 SDK 事实**（对照官方源码/说明）：输出采样率 **22050 Hz**（非 24k）；推理入口 **`model.infer(spk_audio_prompt, text, output_path, lang, emo_vector, stream_return=True)`**（参数名已确认，emotion 向量参数为 `emo_vector`）；**流式输出为 segment 级 torch.Tensor**（非 token 级流式）；情感向量传入前须经 **`model.normalize_emo_vec(vector, apply_bias=True)`**（总强度 ≤ 0.8）；官方 2.5 语言列表为 **ZH/EN/JA/AR/ES**；`spk_audio_prompt` 被直接读取，**无默认音色**。→ 本设计已按此定稿（§5/§7/§10）。

## 架构

```
interaction 进程
├── TtsController      — 协调器：worker 线程 + 队列，emotion 映射 → 合成 → 播放 → 事件发布
│   ├── IndexTTSModel  — IndexTTS2 SDK 封装，懒加载 + LoadGate
│   ├── AudioPlayer    — pyaudio OutputStream 流式播放
│   └── EmotionMapper  — Emotion 枚举 → 8 维向量（经 SDK normalize_emo_vec；neutral → None 不指定情绪）
```

**数据流（修正版）**：

```
DecisionHub → event/reply {text, emotion?}        ← §4 改造后 emotion 随载荷发布
  → InteractionAgent.on_reply()
    → TtsController.speak(text, emotion="neutral")
      → 入队（立即返回；bus 回调不被阻塞）
      → worker: EmotionMapper.to_vector(emotion) → 8 维原始向量或 None
      → IndexTTSModel.synthesize_stream(text, vector, lang) → yield torch.Tensor chunks → 归一化 PCM bytes
      → AudioPlayer.play_stream(chunks) → 首个 chunk 写入声卡前发布 event/tts_speaking（§6）
      → pyaudio 播放
      → 发布 event/tts_finished（仅当 speaking 已发布；含被打断/异常）← 新 topic（§6）
```

## 数据链路改造（前置任务，emotion 增强的前提）

1. **`ReplyPayload` 增加字段**（`payloads.py`）：

   ```python
   class ReplyPayload(TypedDict):
       text: str
       ts: float
       emotion: NotRequired[str]   # Emotion.value；旧发布者不带时默认 neutral
   ```

2. **DecisionHub 发布时带 emotion**（`brain/hub.py:241`）：

   ```python
   self._bus.publish(Topics.REPLY, {
       "text": rendered,
       "ts": reply_ts,
       "emotion": result["emotion"].value,
   })
   ```

3. **`handle_awake_request` 响应带 emotion**（`hub.py:286`，热键路径同样需要）：

   ```python
   return {"text": rendered, "ts": reply_ts, "spoke": spoke,
           "reason": result["reason"], "emotion": result["emotion"].value}
   ```

4. **interaction 侧读取**（`agent.py:78`）：`self._tts.speak(payload["text"], emotion=payload.get("emotion", "neutral"))`。`ReplyPayload` 变更后，旧格式（无 emotion）仍兼容——`payload.get` 兜底。

## 组件设计

### 5.1 TtsController（新文件 `src/yuki/interaction/tts_controller.py`）

- 构造注入：`IndexTTSModel`、`AudioPlayer`、`EmotionMapper`、bus（发布事件用）、config。
- **worker 线程 + latest-job 槽**：只保留一个待处理 job（`Queue(maxsize=1)` 并覆盖旧 pending，或 condition + `_pending` 单槽，优先复用 `_LatestJobWorker` 思路，其线程即 `daemon=True`）；**worker 线程必须 `daemon=True`**——GPU 合成不可取消，join 超时后依赖进程退出回收资源，非 daemon 线程会永久阻止解释器退出；`speak(text, emotion="neutral")` 做完轻量控制后立即返回，worker 串行消费。禁止使用无界队列积累已经过期的回复。打断语义：
  - **generation 计数**：每次 `speak` 时**立即递增**（在途 job 即刻失效，不等 worker 轮询），并覆盖尚未开始的 pending job；worker 在合成前、每次 generator yield 后、播放前均校验 generation/停止标志，过期即关闭 generator 并丢弃。若当前正在播放，`speak()` 同步调用 `player.stop()` 后返回。
  - **`stop()`**：递增 generation + **立即调用 `player.stop()`**（`stop_stream()` 停流、**不关流**，见 §5.3）；`stop()` 需持锁（pyaudio `stop_stream` 非线程安全）。bus 回调线程调 `stop()` 时可能等待 worker 持有的 write 锁，上界约一个 chunk 的 write 时长（≈46ms + PortAudio 缓冲），可接受，但不得在锁内叠加更重操作。
  - **物理现实**：GPU 上的合成一旦开始不可取消——"打断 = 丢弃过期 chunk 流 + 停播"，不是取消 GPU 任务；正在合成的 job 无法中途停止，只能丢弃其产出。
- **事件时机契约**：`event/tts_speaking` 在**首个音频 chunk 实际写入声卡之前**发布（由 `AudioPlayer.play_stream` 在首次 `write()` 前回调触发，见 §5.3）；**没有产出音频（合成失败/空输出）时不发布 speaking**。`event/tts_finished` **仅当 speaking 已发布**才发布（正常结束、被打断、合成中途异常均发，保证 ASR 状态不悬挂），均带 `text`、`ts`；播放期间由 controller 内部维护 active 状态（`is_active` 属性，供健康上报）。
- 模型不可用（LoadGate disabled/degraded）→ 直接控制台打印 `[yuki] {text}`，不发布 speaking/finished 事件（ASR 无需联动）。
- 空文本（strip 后为空）→ 直接 return，不合成不发布。
- **`shutdown()`（供 `InteractionAgent.teardown()` 调用）**，顺序固定：① 在 controller 锁内原子设置 `stopping=True`、递增 generation、清空 pending，并捕获/清除 `_tts_is_active` → ② **`player.stop()`**（先物理停播，解除 worker 阻塞在 `write()` 的可能）→ ③ 若步骤①捕获到 active，发布一次 `event/tts_finished`（必须在停播后发布，且与 worker 的 finally 通过 active 标志保证恰好一次）→ ④ `join(timeout=2s)`。仅当 worker 已退出时才调用 `player.close()`；若超时，记录 warning 并不跨线程 close，依赖进程退出回收资源（worker 为 daemon 线程，见上，进程可正常退出）。worker 在每次 generator yield 后和任何 player 调用前都必须检查 `stopping`，保证超时后也不会再次访问 player。

### 5.2 IndexTTSModel（新文件 `src/yuki/interaction/tts.py`）

- 封装 `from indextts.infer_v2_5 import IndexTTS2`（**懒导入**，模块级无 indextts 依赖，测试/降级环境不装也能跑）。
- LoadGate 管理加载状态（复用 `cognition/load_gate.py`；`enabled=False` → 恒 disabled → 恒 console 降级）。
- **永久配置校验（不经 LoadGate）**：`_load()` 前置校验 `cfg_path`（文件存在）、`model_dir`（目录存在）、`reference_audio_path`（存在且可读），以及锁定版本的最小 checkpoint 集：`gpt.pth`、`s2mel.pth`、`codec.pth`、`multilingual_zh_ja_yue_char_del.tiktoken`、`wav2vec2bert_stats.pt`（另校验 `config.yaml` 引用的本地模型文件），以及 **SDK 构造器硬编码的 `model_dir/hf_cache/` 辅助模型**（`w2v-bert-2.0/`、`campplus_cn_common.bin`、`bigvgan/`——config.yaml 不引用它们，`_referenced_local_files` 扫不到，须显式校验；SDK 缺失时会尝试自动下载，本设计选择确定性校验，首次部署需手工放置或跑一次 SDK）；任一失败 → warning + **`IndexTTSModel` 单独维护 `_config_error: str | None`**（记录具体原因），在 `can_load()` 之前拦截，**进程生命周期内不重试**（区别于瞬时故障的 60s 窗口，见 §9）。方案选择：**不扩展共享 LoadGate**——cognition 的 VLM/STT/VAD 都在用（`vlm.py:52` 等），加永久状态影响面大；配置错误是 IndexTTS 特有需求，局部拦截即可。SDK 直接读取 `spk_audio_prompt`，**没有默认音色**，"缺 ref 用默认声音"不成立。
- `warmup()`：仿 `vlm.py:100-108`，后台 daemon 线程 `_load()`，失败仅 warning 降级；`_load()` 用 `threading.Lock()` 防 warmup 与首次合成竞态。
- `synthesize_stream(text, emotion_vector, ref_audio, lang)` → `Iterator[bytes]`：内部调用（**已核实的官方签名**）：

  ```python
  model.infer(
      spk_audio_prompt=ref_audio,
      text=text,
      output_path=None,
      lang=lang,
      emo_vector=emotion_vector,   # None = 沿用参考音频自然情绪
      stream_return=True,          # segment 级流式，每元素为 torch.Tensor
  )
  ```

  **流式粒度为 segment 级（已确认，非 token 级）**：每段合成完成才返回一个 Tensor——接受首包延迟，"合成完一段播一段"，`play_stream` 接口不变。将每个 Tensor 归一化为 **22050 Hz 16-bit mono PCM bytes** 后 yield。
- `health()`：先取 `gate_health = gate.health()`，再返回 `{"loaded": ..., **gate_health, "config_error": self._config_error, "degraded": bool(self._config_error) or bool(gate_health["degraded"])}`。永久配置错误必须使 health 明确 degraded，不能只附带 `config_error` 而保留 `degraded=False`。
- 显存不足 / 加载异常：`gate.mark_failure()` 后抛异常 → TtsController 捕获 → console 降级；LoadGate 的 `can_load()` 保证 60s 窗口后惰性重试（下次 speak 触发，非定时器）。

### 5.3 AudioPlayer（新文件 `src/yuki/interaction/audio_output.py`）

- 与 IndexTTS SDK 一样采用**懒导入**：模块级不得 `import pyaudio`；仅在 `_ensure_stream()` 首次真正播放时导入并创建 `PyAudio`。默认环境未安装 `pyaudio` 且 `tts.enabled=False` 时，导入 `InteractionAgent`/运行现有测试必须正常；启用后导入失败按播放不可用走 console 降级。
- pyaudio OutputStream：`format=pyaudio.paInt16, channels=1, rate=22050`（与 IndexTTS 2.5 输出一致），`frames_per_buffer` 由 `chunk_size`（样本数）决定。
- `play_stream(chunks, on_first_chunk=None)`：逐 chunk `write()`，边收边播；**首个 chunk 实际写入前调用 `on_first_chunk()`**（controller 在此发布 `event/tts_speaking`）——没有任何 chunk 可写（空/异常）则回调不触发、speaking 不发布。被打断（`stop()`）时抛/返回中断标记，worker 据此放弃后续 chunk。
- `stop()`：持锁 `stop_stream()`（**停流不关流**，流保持可复用）；线程安全。`close()`：关闭流 + 终止 PyAudio，仅 teardown/shutdown 调用。
- **流复用与重建**：`_ensure_stream()` 在每次播放前检查流状态——**`stop_stream()` 后的流处于 `inactive`，需先 `start_stream()` 才能再次 `write()`**；流为 `closed`、或 `inactive` 且无法恢复（设备丢失）时关闭并**惰性重建**。`play_stream` 内部顺序：`_ensure_stream()`（inactive → `start_stream()`）→ 逐 chunk `write()`；write 失败本次降级 console。
- pyaudio 初始化失败（无输出设备等）：error 日志 → 本次降级 console 打印，不影响后续（不标记模型失败，下次仍尝试播放）。
- 注：**`reference_audio_path`（音色克隆参考，模型输入）与 `audio/tts_ref` topic（AEC 回声远端参考）是两回事**。本期不做 AEC，不发布 `audio/tts_ref`；仅当后续接 AEC 时，在 play_stream 内逐 chunk 发布到该 topic。

### 5.4 EmotionMapper（可并入 `tts_controller.py` 或独立小模块）

8 维向量布局：`[高兴, 愤怒, 悲伤, 害怕, 厌恶, 忧郁, 惊讶, 平静]`（与 IndexTTS2 情感标签集一致，实现前对照 SDK 确认维度顺序）。

**官方归一化契约（已核实）**：向量不能直接传给 SDK——官方 WebUI 传入前调用 `model.normalize_emo_vec(vector, apply_bias=True)`，将**总强度限制到 0.8** 并应用偏置。因此：

- `neutral`（及 emotion 缺失）→ 映射为 **`None`**（`emo_vector=None`），**沿用参考音频的自然情绪**，不构造 "neutral" 向量——默认输出即参考音色情绪，文档不称其为 neutral 向量。
- 其余 emotion → 先取下表"原始强度向量"，再统一调用 `model.normalize_emo_vec(vector, apply_bias=True)` 后传入。
- 下表为"原始强度"（未经 SDK 归一化）；**归一化后总强度 ≤ 0.8** 的断言进测试（§11）。

| `Emotion` 枚举值 | 原始强度向量 | 说明 |
|---|---|---|
| `neutral` / 缺失 | `None` | 不传 `emo_vector`，沿用参考音频自然情绪 |
| `joy` | `[1.0,0,0,0,0,0,0,0]` | |
| `anger` | `[0,1.0,0,0,0,0,0,0]` | |
| `sadness` | `[0,0,1.0,0,0,0,0,0]` | |
| `anxiety` | `[0,0,0,0.6,0,0.4,0,0]` | 害怕 + 忧郁 混合（数值可调） |
| `love` | `[0.6,0,0,0,0,0,0,0.4]` | 高兴 + 平静 混合（数值可调） |
| `tired` | `[0,0,0.3,0,0,0.7,0,0]` | 悲伤 + 忧郁 混合（数值可调） |
| 未知/非法值 | `None`（同 neutral） | **必须容错**：`local/router.py:166` 用 `Emotion(str(...))` 构造，无效值会抛 `ValueError`，不能让它炸到 TTS 链路 |

## ASR 联动

- **新增 topic**（`topics.py`）：`TTS_SPEAKING = "event/tts_speaking"`、`TTS_FINISHED = "event/tts_finished"`。
- **AsrSession 新增 `tts` 状态（或 duck 标志），不复用 `speaking`**——`speaking` 语义是"用户正在说话"（VAD），冲突会破坏状态机判断（`asr_session.py:90`、`finish()` 回退逻辑 `asr_session.py:115-127`）。
  - `enter_tts()`：**从任何非 `tts` 状态强制进入 `tts`**（含 `speaking`/`processing`——REPLY 发布后 GPU 合成期间用户可能开口，此时不忽略、不保持原状态），**清空 speech buffer 与 pre-roll**（`feed()` 无条件累积 pre-roll，`asr_session.py:75`——不清空则 TTS 回声留在 pre-roll，下次唤醒被回灌给 STT）；暂停收音触发（不触发 STT）。进入 `tts` 后 `_listening=False`，在途 STT 结果因 `is_current()` 失败被自然丢弃（与"暂停识别"一致）；用户抢话内容丢失属于已接受的 barge-in 范围外损失。
  - **`feed()` 在 `tts`（duck）状态下直接丢弃帧**（不 append pre-roll、不入 speech buffer）；`exit_tts()` 固定执行 `tts → idle` 并再次清空 speech buffer/pre-roll，避免播放尾音进入下一会话。`enter_tts()`/`exit_tts()` 均须幂等：重复 speaking 不重复破坏状态，迟到/重复 finished 不改变非 tts 状态。
  - 新增状态须同步 `check_due()`/`return_to_idle()` 的边界（`tts` 状态下不走超时回退）。
- **订阅归属**：AsrSession 纯逻辑不碰 bus；由 **`build_pipeline()`**（`pipeline.py:540`——`PerceptionPipeline` 没有 `setup()`，订阅在装配函数中完成）订阅 `event/tts_speaking`/`event/tts_finished` 并调用 `enter_tts()`/`exit_tts()`。
- **事件时机**：speaking 仅在**首个音频 chunk 写入声卡前**发布（§5.3）；合成失败/无输出 → 不发布 → ASR 不进入 tts 状态、不无谓关闭识别（GPU 合成期间 ASR 保持可用）。
- **唤醒词风险（明确接受）**：`WakeWordDetector`（`perception/agent.py:68`、`perception/wake_word.py:83`）在 **perception 进程**持续订阅 `audio/mic`，不感知 TTS 播放——本期 TTS 播放期间唤醒词继续监听，**接受 TTS 声音触发误唤醒的风险**（误唤醒后 ASR 处于 `tts` 状态，`feed()` 丢弃帧、无内容可识别，影响有限）；后续阶段可让 perception 订阅 `event/tts_speaking` 抑制唤醒词。
- **本期策略**：TTS 播放期间暂停识别（简化）；AEC 回声消除（`audio/tts_ref`）、FocusManager 抢话打断（barge-in，`agent.py:23-27` 桩）留后续阶段，文档明确范围外。

## 配置

```python
class TtsConfig(BaseModel):
    enabled: bool = False            # 默认关闭：CI/e2e 不装 indextts 也全绿
    cfg_path: str = "checkpoints/config.yaml"     # 永久校验：文件须存在（§5.2）
    model_dir: str = "checkpoints"                # 永久校验：目录须存在（§5.2）
    use_bf16: bool = True            # 需 Ampere+ GPU；CPU 回退极慢，见 §10
    language: Literal["zh", "en", "ja", "ar", "es"] = "zh"  # 官方 2.5 UI 语言列表（无 ko）；自动检测留后续
    reference_audio_path: str = "data/tts/reference_audio/default.wav"  # 音色参考（SDK spk_audio_prompt，硬依赖，缺失即禁用模型）
    chunk_size: int = 1024           # 样本数（≈46ms @22050），pyaudio frames_per_buffer
    retry_window_s: float = 60.0
```

- **删除 `sample_rate` 配置项**：22050 Hz 是 SDK 固定输出，配置项只会带来改错风险（环境变量曾写错 24k）；`AudioPlayer`/`IndexTTSModel` 共用模块常量 `TTS_SAMPLE_RATE = 22050`（定义于 `audio_output.py`）。
- `language` 用 `Literal` 枚举校验，非法值在 `Config` 加载期即报错（Pydantic），不会落到 SDK。

接入点（三处，`config.py`）：

1. `Config` 类加字段：`tts: TtsConfig = Field(default_factory=TtsConfig)`；
2. `Config.load()` 的 section 元组加 `("tts", TtsConfig)`（env 前缀自动 `YUKI_TTS_*`）；
3. `config.example.yaml` 增补 `tts:` 段（enabled 默认 false 的注释说明）。

`enabled` 直接喂给 `IndexTTSModel` 的 LoadGate：`enabled=False → gate.disabled() → 恒 console 降级`。

## 依赖（`pyproject.toml`）

**indextts 未发布到 PyPI**，官方安装方式是 `git clone https://github.com/index-tts/index-tts.git && uv sync`（仓库自带完整依赖环境）。本项目走 pip/editable 流程，采用以下方案（按优先级）：

1. **主选：锁定 commit 的 Git 依赖**（新增独立 `tts` extra，与 `ml` 解耦、按需安装）。本设计锁定经源码契约复核的 commit `ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c`；完整 hash 不得替换为 branch/tag：

   ```toml
   tts = [
     "indextts @ git+https://github.com/index-tts/index-tts.git@ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c",
     "pyaudio",        # Windows 建议 wheel 包（如 pyaudio-wheels）
   ]
   ```

   上游该 commit 含标准 `pyproject.toml` + Hatchling 构建配置，结构上支持 pip Git 直装；开发落地第一步仍需执行依赖解析/导入 smoke test，若因平台依赖导致无法安装则改选 2，不得静默漂移到其他 commit。
2. **备选：vendor**——按锁定 commit 拷贝 SDK 源码到 `third_party/indextts/`（附 commit 版本注记文件），`tts.py` 从该路径导入。完全规避 pip 安装问题，代价是需手动跟进上游修复。
3. （次选）git submodule 固定 commit——引入子模块管理成本，仓库无先例，不推荐首选。

- 默认环境不装：`enabled=False` + 懒导入保证代码路径可测。
- 无论哪种方案，**commit 必须锁定**并记录于依赖文件/文档；SDK 演进快，避免 `infer_v2_5` 接口漂移。

## 错误处理

| 场景 | 处理 |
|---|---|
| 模型加载失败 / GPU 显存不足 | LoadGate `mark_failure` → console 打印 → 60s 窗口后下次 speak 惰性重试 |
| 配置校验失败（`cfg_path`/`model_dir`/参考音频/必需 checkpoint/`hf_cache` 辅助模型任一无效） | warning + **`IndexTTSModel._config_error` 永久拦截（进程生命周期内不重试，不经 LoadGate，§5.2）** → console 降级；health 必须 `degraded=True`；SDK 无默认音色，"缺 ref 用默认声音"不成立 |
| 空文本（strip 后为空） | 直接 return，不合成、不发布事件 |
| 播放期间新 REPLY | `speak` 更新 latest-job 槽并 generation++ → **立即 `player.stop()`（停流不关流）** → 丢弃过期 chunk → 新合成（合成不可取消，见 §5.1） |
| pyaudio 初始化/播放失败 | error 日志 → 本次降级 console → 下次播放 `_ensure_stream()`：inactive → `start_stream()`；closed/不可恢复 → 惰性重建（§5.3） |
| emotion 缺失 / 非法值 | `emo_vector=None`（沿用参考音频自然情绪），容错 `Emotion(str(...))` 抛 `ValueError` 的路径 |
| 合成中途异常 / 无音频产出 | 捕获 → 日志 → 本次降级 console → **仅当 speaking 已发布才发布 `event/tts_finished`**（未发布 speaking 则不发，ASR 从未关闭无需回退） |
| 进程退出（teardown） | `shutdown()` 顺序：**锁内置 stopping/清 pending/清 active → `player.stop()` → active 时发布一次 finished → worker `join(2s)` → 仅在 worker 已退出时 `player.close()`**；join 超时不跨线程 close（§5.1） |

## 实现前验证项（SDK 事实，落地第一步）

**已核实并定稿**（对照官方源码/说明，本设计已按此书写）：

- 安装：非 PyPI 包，官方 `git clone` + `uv sync`（§8 依赖方案已按此修订）。
- 推理入口签名：`model.infer(spk_audio_prompt, text, output_path, lang, emo_vector, stream_return=True)`（§5.2 已按此书写）。
- **流式粒度为 segment 级**（合成完一段返回一个 torch.Tensor，非 token 级流式）——接受首包延迟（§5.2）。
- 输出采样率 **22050 Hz**（§5.3 已改；§7 已删除 `sample_rate` 配置项，改模块常量）。
- 情感向量经 `model.normalize_emo_vec(vector, apply_bias=True)` 归一化，**总强度 ≤ 0.8**；`neutral`/缺失 → `emo_vector=None` 沿用参考音频情绪（§5.4）。
- 官方 2.5 语言列表：**ZH/EN/JA/AR/ES**（§7 已用 `Literal` 校验，去掉 ko）。
- `spk_audio_prompt` 被直接读取，**无默认音色**（§5.2/§9 已改为"配置校验失败即禁用模型"）。

补充已确认事实：锁定 commit 中模块/类名为 `from indextts.infer_v2_5 import IndexTTS2`；构造参数包含 `cfg_path`、`model_dir`、`use_bf16`；emotion 顺序为 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`，`normalize_emo_vec` 会应用固定 bias 并把总强度缩放到不超过 0.8。

**仍待运行时验证**（不改变上述接口设计）：

1. **chunk tensor 形状/连续性**：用锁定 commit + checkpoint 做一次最小推理，记录实际 batch/channels 维度与 dtype，确定归一化（`detach().cpu().contiguous()` → clamp/cast `int16` → flatten/tobytes）细节，并断言 mono。
2. **依赖/导入 smoke**：`pip install -e ".[dev,windows,tts]"` 后验证 `from indextts.infer_v2_5 import IndexTTS2`，确认 Windows 下 torch/torchaudio/PyAudio 解析无冲突。
3. **bf16 硬件 smoke**：支持设备验证 bf16；非支持设备显式配置 `use_bf16=False`。Windows CPU 仅作调试，合成极慢。

## 测试要点

- 正常合成与播放（`AudioPlayer` 用 FakeStream 注入，不碰真实声卡）。
- 模型加载失败 → LoadGate degraded → console 降级（`[yuki]` 前缀断言，e2e 兼容）。
- `enabled=False` → 恒 console，健康上报 degraded（`health_components` 透传 `IndexTTSModel.health()`，替换 `agent.py:112` 硬编码）。
- emotion 可选：REPLY 不带 emotion / `neutral` / 非法值 → **`emo_vector=None`**（沿用参考音频情绪，FakeModel 断言收到 None，而非零向量）。
- **数据链路**：hub 发布 REPLY 含 `emotion`；旧格式载荷（无 emotion）兼容。
- ASR 联动：`event/tts_speaking` → AsrSession `tts` 状态（**断言新状态名，非 `speaking`**）；`event/tts_finished` → 回退；合成异常且 speaking 已发布 → 也发布 finished（状态不悬挂）。
- **事件时机**：合成失败/空输出 → **不发布 speaking**（ASR 不关闭）；speaking 在首个 chunk 写入前发布（FakeStream 断言回调与 `write()` 的相对顺序）。
- 播放期间新 REPLY → generation 递增 + **立即 `player.stop()`（FakePlayer 断言 stop 调用时机）** → 旧流丢弃。
- **生命周期**：`shutdown()` 断言 **置 stopping/清 active → player.stop → finished（active 时恰好一次）→ join → close** 的相对顺序；finished 不得早于物理停播。join 超时断言不调用 `player.close()`，且 worker 后续不再访问 player。**worker 线程 `daemon=True`**（断言 `thread.daemon is True`）。
- 配置校验失败（`cfg_path`/`model_dir`/参考音频/任一必需 checkpoint/`hf_cache` 辅助模型无效）→ `_config_error` 永久拦截（**多次 speak 不重试**）+ console 降级 + health 含 `config_error` 且 `degraded=True`。
- **emotion 归一化**：非 neutral 向量经 `normalize_emo_vec(apply_bias=True)` 后**总强度 ≤ 0.8**（FakeModel 断言）；neutral/缺失 → `emo_vector=None`。
- **ASR 抑制**：`enter_tts()` 清空 pre-roll 与 speech buffer；**从 `speaking`/`processing` 状态进入同样强制 `tts` 并清空**（在途 STT 结果被丢弃）；`tts` 状态下 `feed()` **直接丢弃帧**（pre-roll 不增长）；`exit_tts()` 固定回 idle 并清空缓冲；enter/exit 重复调用幂等。
- **流复用**：`stop()` 后流为 inactive → 下次 `play_stream` 先 `start_stream()` 再 write（FakeStream 断言调用序列）；closed → 惰性重建。
- **可选依赖**：未安装 `pyaudio` 时仍可导入 interaction 相关模块、运行 `enabled=False` 路径；首次实际播放才触发懒导入，失败后本次 console 降级。
- `language` 非法值（如 `ko`）→ Config 加载期 Pydantic 校验失败。
- 空文本不合成、不发布事件。
- **latest-job**：连续快速调用 `speak(A/B/C)` 时只保留 C；A 在途时 generation 失效，B pending 被 C 覆盖，无界队列不增长；旧 job 失败时也不得打印或发布成当前回复。
- `speak()` 不执行合成/阻塞播放；除一次受锁保护的 `player.stop()` 外立即返回。

## 范围外（后续阶段）

- AEC 回声消除（`audio/tts_ref` 发布与采集侧远端参考接入）。
- FocusManager 真实抢话打断（barge-in 检测）。
- TTS 流式/过渡回复（L2 长回复的"让我想想"过渡语，`l2-cloud-bridge-design.md` 已留口）。
- TTS 能力开放为工具（function-call-framework 范围外项）。
