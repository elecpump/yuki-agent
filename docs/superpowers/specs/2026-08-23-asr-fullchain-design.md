# Design: ASR 全链路修订版（基于现有代码的增量方案）

Date: 2026-08-23

## Goal

在现有双进程架构上补齐 ASR 全链路的两个真正缺口——**唤醒词检测**与**唤醒后回落 IDLE 的状态管理**，同时修正原设计草案中与现状矛盾、以及"更复杂但不更优"的选型。

## 总体判断

原设计骨架成立，且约 70% 已在代码中实现。修订版坚持以下三条：

1. **保留 SenseVoice-Small 作为 STT**，不换 Paraformer-large（单模型自带标点/情感/中英混合，轻量、快，中文准确率已达标）。
2. **保留 ZeroMQ + protobuf 总线传输**，不做跨进程零拷贝共享内存（与现有总线架构冲突；先通过 proto typed bytes 字段砍掉 base64 33% 开销即可）。
3. **cognition 保持常驻进程**，不做"按需拉起"（模型预热秒级，会破坏 `<500ms` 延迟目标）。

新增/修正的范围收敛为五块：唤醒词检测器、唤醒前音频 pre-roll、ASR 状态机（含回 IDLE 路径）、状态机驱动的唤醒/续听超时、以及 STT 异步结果的会话校验。

## 现状映射（已实现，勿重复建设）

| 设计组件 | 现状代码 | 状态 |
|----------|----------|------|
| AudioCapture 20ms 帧 → `audio/mic` | `src/yuki/perception/audio.py` | ✅ 已实现 |
| 双进程 perception / cognition | `ProcessAgent` 体系（`perception/agent.py`、`cognition/agent.py`） | ✅ 已实现 |
| VAD + 静音 15 帧 + 最长 10s | `cognition/speech_buffer.py`（`silent_frames=15`、`max_utterance_s=10`） | ✅ 已实现 |
| STT 线程 + 发布 `event/perception/user_utterance` | `cognition/pipeline.py`（`_stt_worker`、`_recognize_utterance`） | ✅ 已实现 |
| 唤醒后 gate | `on_awake` 置 `_listening=True`（`pipeline.py:346`） | ⚠️ 无唤醒词来源、无超时回落、无会话隔离 |
| 唤醒前音频缓存 | 无 | ⚠️ 必补；否则“Yuki，帮我……”会丢掉唤醒词后的前半句 |
| `event/awake` 语义 | pipeline 订阅 topic；热键走 `cognition.awake` RPC 并额外触发 hub | ⚠️ 不能简单宣称唤醒词与热键语义完全一致 |
| openWakeWord 输入适配 | `audio/mic` 当前是 20ms float32 base64 | ⚠️ 需聚合/转换为 openWakeWord 所需的 16kHz int16 PCM 窗口 |
| 后处理（标点） | SenseVoice 输出自带标点 | ➖ 不需要独立后处理层 |

## 修订后的状态机

```
                    ┌─────────────┐
                    │   IDLE      │
                    │ (待机监听)   │
                    └──────┬──────┘
                           │
               唤醒词 "yuki" / 热键
                 (source=wake_word|hotkey)
                           │
              建立 session_id，注入 pre-roll
                           │
                           ▼
                    ┌─────────────┐
                    │  LISTENING  │
                    │ (监听中)     │
                    └──────┬──────┘
                           │
                    VAD 检测到语音活动
                           │
                           ▼
                    ┌─────────────┐
               ┌───▶│  SPEAKING   │
               │    │ (说话中)     │
               │    └──────┬──────┘
               │           │
               │    VAD 静音持续 15 帧
               │           │
               │           ▼
               │    ┌─────────────┐
               │    │ PROCESSING  │
               │    │ (识别中)     │
               │    └──────┬──────┘
               │           │
               │    STT 完成 + session 校验 + 发布结果
               │           │
               └───────────┘
             回到 LISTENING（续听窗口 listen_window_s）
                     │
                     │  窗口内无语音 → 超时
                     ▼
               ┌─────────────┐
               │   IDLE      │  ←── 关键补丁：原设计缺失的回 IDLE 路径
               └─────────────┘
```

回 IDLE 的两种触发：

1. **唤醒超时**：进入 LISTENING 后 `listen_timeout_s`（默认 10s）内未检测到任何语音 → 回 IDLE，重新武装唤醒词。
2. **续听超时**：发布结果后进入续听窗口 `listen_window_s`（默认 5s），窗口内无新语音 → 回 IDLE。

实现要点（`cognition/pipeline.py`）：

- `on_awake` 只接受在 IDLE 状态的唤醒；重复唤醒在非 IDLE 状态直接忽略（或刷新监听超时）。
- `on_mic` 即使在 IDLE 也维护一个固定长度的 rolling pre-roll（默认 1.0-1.5s）；进入 LISTENING 时把唤醒点附近的 pre-roll 注入 `SpeechBuffer`，避免丢掉“Yuki，帮我……”里的前半句。
- 用 `_session_id`/`_asr_generation` 标识一次唤醒会话；`_on_utterance` 提交 STT job 时携带 session，`_recognize_utterance` 完成时必须确认 session 仍有效，过期结果直接丢弃。
- 用一个 `_last_activity_monotonic` 时间戳 + 看门狗线程（复用 `_deep_timer_thread` 的模式）做超时回落；回落 IDLE 时递增 generation，让已经在跑的 STT 结果失效。
- 状态转移集中在一个 `_asr_state` 字段（`idle/listening/speaking/processing`），但 `processing` 只描述“上一段 utterance 正在识别”。如果要在 STT 段 N 期间继续 VAD 累积段 N+1，`on_mic` 仍需在 `processing` 下喂 buffer，不能把 `processing` 当成停止收音状态。

## 线程模型（修订）

| 线程 | 职责 | 运行位置 | 备注 |
|------|------|----------|------|
| AudioThread | 音频采集，20ms 帧 | perception，CPU | 已存在 |
| WakeWordThread | openWakeWord 唤醒词检测 | perception，CPU | **新增**；订阅 `audio/mic`，先做 20ms float32 → 80ms int16 PCM 适配 |
| VAD | 语音活动检测 | cognition，总线订阅线程，CPU | 已存在（`SpeechBuffer.add_frame` 内联）；**不用 GPU**，Silero 单帧 CPU ~1ms，GPU 调度反而贵 |
| STTThread | SenseVoice 推理 | cognition，GPU | 已存在（`_stt_worker`，`_LatestJobWorker` 丢弃旧任务） |
| Watchdog | 唤醒/续听超时回落 IDLE | cognition，CPU | **新增**，低优先级 |

关键点：

- **VAD 放 CPU、STT 放 GPU**——避免 torch 双模型共享 GPU 的流管理复杂度（原设计"GPU 队列调度"不划算）。
- **真正的并行**：STT 推理段 N 期间，`SpeechBuffer` 继续 VAD 累积段 N+1（`_flush` 后 buffer 清空、`on_mic` 继续喂）。这要求状态机允许 `processing` 状态继续接收音频帧。
- **唤醒词与 VAD/STT 的职责隔离**：perception 只负责检测唤醒词并发 wake 事件；cognition 只在 listen gate 打开后产出用户 utterance。唤醒词检测本身常驻运行，但 ASR 结果发布由 cognition 的 session/generation 防串。

## 进程模型（修订）

- **perception 进程**（常驻、低延迟）：音频采集 + 唤醒词检测，检测到唤醒词发布 `event/awake`。
- **cognition 进程**（常驻、计算密集）：VAD、语音缓冲、STT 推理、发布识别结果。**维持常驻**——模型加载与预热成本高，按需拉起会破坏延迟目标。

## 技术选型（修订）

| 组件 | 技术选择 | 理由 |
|------|---------|------|
| 唤醒词检测 | openWakeWord（CPU，`onnxruntime`） | 开源、轻量；需调阈值控误唤醒；必须配置可用的 “yuki” 自定义模型 |
| VAD | webrtcvad（保持默认） | 已实现、可测、零额外依赖；Silero 作为后续可选升级（如需更高精度再引入） |
| STT | SenseVoice-Small（funasr，GPU） | 已实现；中英混合、自带标点/情感，单模型覆盖"STT+后处理"，比 Paraformer-large + ct-punc 更轻 |
| 后处理 | 无独立层 | SenseVoice 输出已含标点；如未来切换无标点模型再补 |
| 传输 | ZeroMQ + protobuf（保持） | 与现有总线一致；base64 33% 开销留待 proto typed bytes 字段消除 |

### 唤醒词 → cognition 的传递

复用现有 `Topics.AWAKE`（`event/awake`）作为 **ASR listen gate 事件**，`build_pipeline` 已订阅它（`pipeline.py:471`）。唤醒词检测器发布：

```json
{"source": "wake_word", "ts": 123.45, "score": 0.83, "model": "yuki"}
```

需要明确的是：这与热键路径并非完全同义。热键当前走 `cognition.awake` RPC，`CognitionRuntime.handle_awake_request` 会先调用 `pipeline.on_awake(...)`，再调用 `hub.handle_awake_request(...)`；而 `event/awake` topic 目前只被 pipeline 订阅。因此本期定义为：

- `event/awake`：打开 ASR gate，不直接触发 brain 回复。
- `cognition.awake` RPC：热键/外部主动唤醒入口，保留现有“pipeline + hub”协调语义。

如果未来希望唤醒词也触发 hub 的 AWAKE 决策，应让 perception 调 `cognition.awake` RPC，或在 cognition 侧新增明确的 awake coordinator；不要让 `event/awake` 隐式承担两种语义。

### openWakeWord 输入适配

当前 `AudioCapture` 发布的是 20ms、16kHz、float32、base64 编码帧。按 openWakeWord README 的在线推理接口约束，`WakeWordDetector` 不能直接把该 payload 喂给模型，需要一个可测的适配层：

1. 解码 `payload["pcm"]` 为 float32 numpy 数组。
2. 按 openWakeWord 在线推理所需窗口聚合为 80ms 倍数（默认聚合 4 个 20ms 帧）。
3. clip 到 `[-1.0, 1.0]` 后转换为 int16 PCM。
4. 调用注入的 wake backend，超过 `threshold` 且超过 `refractory_s` 才发布 `event/awake`。

`WakeWordDetector` 构造函数必须支持 fake backend 和 fake clock，测试不依赖真实模型。

## 性能目标（保持）

- 延迟：说完后 `<500ms` 出最终结果（SenseVoice-Small GPU 可满足；模型常驻不重复加载）
- 准确率：中文识别准确率 >95%
- 误唤醒率：<0.01 次/小时（openWakeWord 阈值调参 + 唤醒后若无语音由超时自动回落兜底）
- 模型大小：唤醒词 <10MB，SenseVoice-Small ~1GB（远小于原设计 Paraformer 的 ~2GB）

## 数据流（修订）

1. **音频采集**：`AudioCapture` 以 20ms 帧发布 `audio/mic`
2. **pre-roll 缓存**：cognition 的 `on_mic` 始终维护最近 1.0-1.5s 的 rolling audio；IDLE 时不做 VAD/STT
3. **唤醒词检测**：`WakeWordDetector` 订阅 `audio/mic`，聚合并转换为 int16 PCM，检测到 "yuki" 发布 `event/awake`（`source=wake_word`）
4. **状态转移**：cognition 收到 `event/awake` → IDLE→LISTENING，创建 `session_id`，重置 `SpeechBuffer`，注入 pre-roll，启动唤醒超时
5. **VAD 缓冲**：`SpeechBuffer` 累积语音帧，静音 15 帧或最长 10s 触发整段 utterance
6. **STT 推理**：`SpeechRecognizer` 识别（SenseVoice，自带标点），任务携带 `session_id`
7. **结果发布**：STT 完成后先校验 `session_id` 仍有效；有效则发布 `event/perception/user_utterance`，回 LISTENING 进入续听窗口
8. **回落**：续听窗口无语音 → IDLE，递增 generation，让旧 STT 结果失效，重新等待唤醒

## 错误处理（修订）

- **唤醒词检测失败**：不发布 `event/awake`，系统保持 IDLE。
- **误唤醒**：会进入 LISTENING；若后续无语音，由唤醒超时自动回落 IDLE。
- **VAD 异常**：保持现状——`SpeechBuffer.add_frame` 捕获异常并跳过该帧
- **STT 推理失败/空文本**：记录日志，不发布用户 utterance；必须在 `finally` 中按当前 session 回 LISTENING 或 IDLE，不能因为空文本直接 `return` 而卡在 PROCESSING。
- **STT 过期结果**：如果 watchdog 已回 IDLE 或新唤醒已创建新 session，旧 STT 结果直接丢弃。
- **GPU 不可用**：funasr 回退 CPU 推理（延迟升到 ~2s），流程不断

## 工作分解

1. **config.py / config.example.yaml** — 新增 `WakeWordConfig`，挂 `Config.wake_word`，并加入 env 映射。字段建议：
   - `enabled: bool = False`（默认关闭，避免缺模型时影响现有运行）
   - `model_path: str = ""` 或 `model_paths: list[str]`
   - `threshold: float = 0.5`
   - `refractory_s: float = 2.0`
   - `chunk_ms: int = 80`
   - `pre_roll_s: float = 1.2`
   - `listen_timeout_s: float = 10.0`
   - `listen_window_s: float = 5.0`
2. **cognition/pipeline.py** — 先补 ASR 状态机、pre-roll、watchdog、session/generation 校验。即使 wake word 还没接入，热键路径也能先获得超时回落与过期 STT 防护。
3. **perception/wake_word.py** — `WakeWordDetector` + `WakeWordFrameAdapter`：
   - 订阅 `audio/mic`
   - 解码 20ms float32 base64
   - 聚合为 `chunk_ms`
   - 转 int16 PCM
   - 调注入式 backend
   - 超阈值且超过 refractory 后发布 `event/awake`
4. **perception/agent.py** — 当 `config.wake_word.enabled` 为 true 时装配 `WakeWordDetector`；`health_components` 增加 `wake_word` 项，teardown 停止 detector。
5. **pyproject.toml** — 新增 ASR/唤醒可选依赖，优先放进 `ml` 或新增 `asr` extra，而不是基础依赖；例如 `openwakeword`、`onnxruntime`。是否同时进 `windows` 取决于本地安装体验。
6. **测试**：
   - `tests/perception/test_wake_word.py`：音频格式适配、阈值、refractory、fake backend、disabled 不发布。
   - `tests/cognition/test_asr_state_machine.py`：IDLE/LISTENING/SPEAKING/PROCESSING 转移、唤醒超时、续听窗口、重复唤醒、pre-roll 注入、过期 STT 丢弃、空文本不死锁。
   - 更新既有 `tests/cognition/test_pipeline.py`，保证 `mic_before_awake` 仍不触发 STT，但 pre-roll 会缓存帧。

## 验证

- `pytest` 全量绿
- 新增状态机单测覆盖所有状态转移与两个超时回落路径
- e2e 冒烟：真实麦克风说 "Yuki，帮我记录一下" → 触发识别且不丢“帮我”前缀；静默 10s → 状态回落 IDLE
- 唤醒词模型验收：在目标麦克风/房间噪声下记录误唤醒率、漏唤醒率、平均触发延迟；未达标只调 `threshold`/`refractory_s` 或更换模型，不改 ASR 状态机

## Out of Scope

- 不做跨进程零拷贝共享内存（后续 proto typed bytes 单独处理）
- 不换 Paraformer-large / 不加独立标点模型
- 不引入 Silero VAD（作为后续可选升级记录，不进本期）
- 不做多段语音流式合并（`_LatestJobWorker` 的丢旧保新语义保持）
- 不在本期训练唤醒词模型；但启用 `wake_word.enabled=true` 前必须配置可用的 “yuki” 模型并完成上面的模型验收
