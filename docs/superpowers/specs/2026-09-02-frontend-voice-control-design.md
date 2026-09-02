# Design: 前端热键与按钮触发语音交互

Date: 2026-09-02

关联文档：

- [2026-08-23-asr-fullchain-design.md](2026-08-23-asr-fullchain-design.md)
- [2026-08-23-desktop-gateway-design.md](2026-08-23-desktop-gateway-design.md)
- [2026-08-30-yuki-desktop-frontend-design.md](2026-08-30-yuki-desktop-frontend-design.md)

## 1. 任务分析

### 1.1 需求解释

本文将用户所说的“语言功能”解释为“语音功能”：用户可以在 Yuki 桌面前端中点击麦克风
按钮，或按下快捷键，开始一次语音监听；再次点击或再次按键可取消当前监听。

实现遵循以下工作顺序：任务分析 → 文档编写并由用户确认 → 代码编写 → 代码审查。
本文完成后必须暂停，不能在未获得用户确认时进入代码阶段。

### 1.2 用户目标

- 鼠标用户可以从聊天输入区直接开始语音交互。
- 键盘用户可以通过固定快捷键触发相同能力。
- 前端明确显示空闲、监听、说话、识别和 TTS 播放状态，不让用户猜测是否已经生效。
- 前端入口复用当前 Python 运行时的麦克风、VAD、STT、cognition 与 TTS 链路。
- 控制失败、后端不可用或语音能力未启用时，前端给出可理解的错误提示。

### 1.3 现状

当前语音链路已经存在：

```text
AudioCapture
  → Topics.MIC
  → PerceptionPipeline.on_mic
  → AsrSession / SpeechBuffer / VAD
  → SpeechRecognizer
  → Topics.USER_UTTERANCE
  → DecisionHub
  → Topics.REPLY
  → InteractionAgent / TTS
```

已有两个开始监听的入口：

- `WakeWordDetector` 检测到唤醒词后发布 `Topics.AWAKE`。
- `InteractionAgent` 注册名为 `trigger` 的 `HotkeyManager` handler，请求
  `cognition.awake` 服务。

但是 `HotkeyManager` 目前只是进程内 handler 字典，并没有接入真实 Windows 全局热键；前端也
没有语音按钮、语音控制 API 或 ASR 状态展示。Gateway 的 `/ws/perception` 只推送前台窗口和
文本感知信息，`/ws/chat` 只处理由该 WebSocket 发起的文字聊天请求。语音识别出的用户文本及其
回复不会自动显示到当前聊天面板。

### 1.4 术语边界

`voice`、`idle`、`listening`、`speaking`、`processing`、`tts`、`active` 和
`available` 都是语音交互的运行时/API 状态，不是 Thread、记忆、关系或人格领域概念，
因此本功能不扩展 `CONTEXT.md` 的领域词汇。代码、测试和 UI 状态名称应使用本文定义，
不得为同一运行时状态另造领域术语。

### 1.5 缺口

要让前端可靠地控制现有语音链路，需要补齐：

1. cognition 对外提供开始、取消和查询 ASR 会话状态的固定服务。
2. Gateway 把固定服务转换为窄化的 REST API，不暴露任意 bus service 调用。
3. React 增加语音状态、控制 hook、麦克风按钮和窗口内快捷键。
4. 前后端统一状态与错误契约，并覆盖重复触发、超时、断线和组件卸载。

## 2. 范围

### 2.1 本次实现

- 聊天输入框右侧增加麦克风按钮。
- 桌面窗口处于前台时，`Ctrl+Shift+Space` 触发或取消监听。
- 开始监听后展示 `正在聆听…`；检测到语音、识别中或 TTS 播放时展示对应状态。
- 监听中的按钮使用激活样式，并提供无障碍名称和按下状态。
- Gateway 提供语音控制与状态 API。
- cognition 暴露线程安全的 ASR 状态快照与显式取消能力。
- 保留唤醒词和已有进程内 trigger 的行为；所有入口共享同一个 `AsrSession` 状态机。

### 2.2 不在本次范围内

- 不在 WebView 中调用 `getUserMedia()`，不把音频流上传到 Gateway。
- 不替换现有 `AudioCapture`、VAD、STT 或 TTS 模型。
- 不实现系统级全局快捷键；快捷键只在 Yuki 窗口处于前台时有效。
- 不允许用户在 UI 中修改快捷键。
- 不在本次把语音识别文本和语音回复镜像进聊天消息列表。
- 不修改 `config.yaml`，也不持久化语音开关状态。

系统级全局快捷键需要 Tauri global-shortcut 插件、权限声明和冲突注册处理，属于独立能力。
当前需求使用前端窗口级热键即可满足，并避免扩大原生权限面。

## 3. 核心决策

1. **复用后端麦克风。** Tauri 前端只发送控制命令；音频仍由 perception 进程采集，避免双重
   占用麦克风、浏览器权限弹窗和两条格式不同的音频链路。
2. **使用显式状态契约。** 前端不根据按钮点击时间猜测状态，而是读取 cognition 的
   `AsrSession` 快照。
3. **控制为幂等语义。** 空闲时 start 进入 listening；已经处于 listening/speaking/processing
   时再次 start 返回当前快照，不重置缓冲区或创建第二个会话；cancel 可重复调用。
4. **热键和按钮共用一个 action。** 两个入口都调用同一个 `toggleVoice()`，保证并发锁、错误
   处理和 UI 行为一致。
5. **窗口内快捷键选用 `Ctrl+Shift+Space`。** 它不与现有 Enter 发送、Shift+Enter 换行冲突；
   即使焦点位于 textarea 中也生效，并调用 `preventDefault()` 防止插入空格。
6. **短轮询覆盖活跃期和 TTS。** `frontend/src/config/runtime.ts` 新增
   `VOICE_POLL_MS = 500`，start 成功后按该常量查询状态；`active=true` 或状态为 `tts` 时继续
   轮询，只有回到 `idle` 后才停止。这样无需为单一控制状态扩展 WebSocket channel，也不会
   在空闲时持续请求。

## 4. 状态模型

对外状态以 `AsrSession` 的五个现有状态值为基础，并新增派生字段 `active`、`available`：

- `idle`：未监听。
- `listening`：等待用户开始说话。
- `speaking`：VAD 已检测到语音并正在收集。
- `processing`：一句话已结束，STT 正在识别。
- `tts`：Yuki 正在播放语音，ASR 为防止回声而暂停。

统一快照：

```json
{
  "available": true,
  "state": "listening",
  "session_id": 12,
  "active": true
}
```

规则：

- `active=true` 当且仅当状态为 `listening`、`speaking` 或 `processing`。
- `tts` 不视为可取消的监听会话；按钮禁用并显示“Yuki 正在说话”，但前端必须继续按
  `VOICE_POLL_MS` 轮询，直到 `exit_tts()` 后观察到 `idle`。
- `available=false` 表示运行时没有可用的语音控制服务或 STT 配置禁用。
- `session_id` 只用于诊断和防止旧状态覆盖新状态，不由前端生成。

状态转换：

```text
idle --start--> listening
listening --检测到语音--> speaking
speaking --语句结束--> processing
processing --STT 完成且无残留语音--> listening
processing --STT 完成但仍有残留语音--> speaking
listening --当前等待窗口超时--> idle
listening/speaking/processing --cancel--> idle
任意非 tts 状态 --TTS speaking--> tts --TTS finished--> idle
```

首次 start 后的 `listening` 使用 `listen_timeout_s`；一次 STT 完成后回到 `listening`，改用
`listen_window_s` 等待用户继续说话。识别完成本身不结束会话，只有续听窗口超时、显式 cancel
或 TTS 状态转换会令该会话退出。

取消 processing 时，后台 STT 工作可以自然结束，但当前 session 立即失效，完成结果不得再发布为
`Topics.USER_UTTERANCE`。现有 `_recognize_utterance()` 的 session 校验可以保证这一点。

## 5. 后端设计

### 5.1 `AsrSession`

增加线程安全方法：

```python
def snapshot(self) -> dict: ...
def cancel(self) -> dict: ...
```

`snapshot()` 在同一把锁内返回 `state` 与 `session_id`。`cancel()` 调用现有 idle 收敛逻辑并返回
新快照；处于 `tts` 时保持 `tts`，不打断 TTS。

`begin()` 保持已有返回 pre-roll frames 的接口，避免扩大现有测试和调用面的改动。是否真正进入
新会话可由 begin 前后的快照判断。

### 5.2 `PerceptionPipeline` 与 cognition 服务

`PerceptionPipeline` 增加：

```python
def voice_status(self) -> dict: ...
def cancel_voice(self) -> dict: ...
```

`CognitionRuntime` 注册三个固定服务：

```text
cognition.voice.start
cognition.voice.cancel
cognition.voice.status
```

- start 复用 pipeline 的 `on_awake()`，`source` 固定为 `frontend`，但不调用
  `DecisionHub.handle_awake_request()`；AWAKE 本身是 silent trigger，前端控制只需要启动 ASR。
- cancel 调用 pipeline 的显式取消方法。
- status 返回当前快照。

现有 `cognition.awake` 服务、唤醒词发布和 InteractionAgent trigger 保持不变。

服务返回时补充 `available`。当 `stt.enabled=false` 时 start 返回稳定错误
`voice_unavailable`；status 返回 `available=false` 和 `idle`，前端按钮保持禁用。

### 5.3 Gateway API

新增固定接口：

```text
GET    /api/voice
POST   /api/voice/listen
DELETE /api/voice/listen
```

成功响应均为第 4 节的状态快照。start/cancel 是同步、短时 bus request，不创建后台 operation。

错误映射：

- cognition 服务不存在或进程不可达：`503 voice_unavailable`。
- bus 请求超时：`504 voice_timeout`。
- 其他未预期错误：保留现有 `500 internal_error` envelope。

Gateway 不接受 service 名、topic 名或任意 action 参数，避免形成通用内部 bus 代理。

实现新增 `VoiceControlError(code, message)`，由 `GatewayRuntime` 将 `BusTimeoutError`、其他
`BusError` 和 cognition 的 `voice_unavailable` 结果归一为上述稳定错误码；
`create_gateway_app()` 仿照现有 `LocalModelControlError` 注册专用 exception handler，将
`voice_unavailable` 映射为 503、`voice_timeout` 映射为 504。不能依赖现有 `HTTPException`
handler，因为它只为 400/404/408 定义稳定 code。

## 6. 前端设计

### 6.1 状态与 API

在 `frontend/src/types/api.ts` 增加 `VoiceState` 与 `VoiceStatus`，在 REST client 之上增加三个窄化
函数：读取状态、开始监听、取消监听。

新增 `voiceSlice.ts` 保存：

- `voiceStatus`
- `voicePending`
- `voiceError`

新增 `useVoiceControl()`：

- 首次挂载读取状态。
- 暴露 `toggleVoice()`。
- 注册和清理 `window.keydown` listener。
- `active=true` 或状态为 `tts` 时按 `VOICE_POLL_MS` 轮询，观察到 idle 后停止。
- 使用单次请求锁，阻止按钮与热键同时触发重复请求。
- `voicePending` 只表示 start/cancel 控制请求；后台 GET 轮询不触发按钮 loading 或禁用。
- 轮询与控制请求重叠时，以递增 request generation 丢弃旧响应，控制结果优先。
- 忽略卸载后完成的请求，避免 StrictMode 下旧请求更新新实例状态。

### 6.2 组件

`ChatInput` 新增语音相关 props，在发送按钮左侧渲染麦克风按钮：

- idle：麦克风图标，tooltip 为“开始语音（Ctrl+Shift+Space）”。
- listening/speaking/processing：高亮或脉冲样式，点击取消。
- tts：禁用，tooltip 为“Yuki 正在说话”。
- unavailable：禁用，tooltip 展示不可用原因。

提示行优先级为：聊天 disabled reason → voice error → voice state hint → 默认键盘提示。文字输入和
发送按钮继续遵循现有聊天 WebSocket 状态；语音按钮不依赖 `/ws/chat` 是否连接，因为语音回复走
现有 `Topics.REPLY`/TTS 链路。

按钮必须提供：

```text
aria-label="开始语音" / "取消语音"
aria-pressed=true|false
```

### 6.3 快捷键行为

- 监听 `window` 的 `keydown`。
- 仅匹配 `event.ctrlKey && event.shiftKey && event.code === "Space"`。
- 忽略 `event.repeat`，避免长按连续切换。
- 匹配后执行 `preventDefault()` 并调用 `toggleVoice()`。
- 组件卸载时移除 listener。

### 6.4 失败与并发处理

- start 请求失败：恢复上一次服务端状态，显示错误，不进入伪 listening。
- cancel 请求失败：继续轮询服务端真实状态，不在本地强制改成 idle。
- 状态轮询临时失败：保留最后状态并展示连接错误；连续请求不会叠加。
- 同一时刻按钮点击和快捷键触发：前端请求锁只允许一个请求；后端 start/cancel 仍为幂等。
- 用户说话期间再次触发：执行 cancel，session generation 递增，迟到的 STT 结果被丢弃。
- TTS 播放期间触发：不打断 TTS，返回当前 tts 状态。
- 页面刷新：首次 GET 恢复真实状态，不假设 idle。

## 7. 预计修改范围

后端：

- `src/yuki/cognition/asr_session.py`
- `src/yuki/cognition/pipeline.py`
- `src/yuki/cognition/assembly.py`
- `src/yuki/bus_server/gateway.py`

前端：

- `frontend/src/types/api.ts`
- `frontend/src/api/rest.ts`
- `frontend/src/config/runtime.ts`
- `frontend/src/state/slices/voiceSlice.ts`（新增）
- `frontend/src/state/store.ts`
- `frontend/src/state/hooks/useVoiceControl.ts`（新增）
- `frontend/src/components/chat/ChatInput.tsx`
- `frontend/src/components/chat/ChatPanel.tsx`
- `frontend/src/App.tsx`
- `frontend/src/styles/global.css`

测试：

- `tests/cognition/test_asr_session.py`
- `tests/cognition/test_pipeline.py`
- `tests/cognition/test_assembly.py`
- `tests/bus_server/test_gateway.py`
- `frontend/tests/client.test.ts`
- `frontend/tests/voiceSlice.test.ts`（新增）
- `frontend/tests/useVoiceControl.integration.test.tsx`（新增）
- `frontend/tests/ChatInput.test.tsx`

## 8. 实现顺序（每步可验证）

1. **ASR 状态契约**：先为 `AsrSession` 写 snapshot/cancel 失败测试，再实现线程安全快照、取消
   和迟到识别失效；运行 `test_asr_session.py`。
2. **cognition 固定服务**：为 pipeline/runtime 写 start/status/cancel 服务测试，再注册三个固定
   service；运行 pipeline/assembly 测试。
3. **Gateway REST**：实现 `VoiceControlError`、三个 REST 接口和 503/504 错误映射；运行
   gateway 测试。
4. **前端数据层**：增加 DTO、REST client、`VOICE_POLL_MS`、slice 与 hook，覆盖 active/tts
   轮询、卸载清理、重复触发和错误恢复。
5. **前端 UI**：加入麦克风按钮、快捷键、状态提示、无障碍属性和样式；运行组件测试。
6. **集成验证**：依次运行聚焦测试、前端全量测试/build、Python 默认测试与 Windows 手工
   冒烟。
7. **代码审查**：验证完成后进入第 11 节的独立审查阶段；先修正发现的问题并回归测试，再
   向用户交付。

每一步保持可独立回归；后一步不以修改前一步测试断言来掩盖状态机错误。

## 9. 测试策略

### 9.1 Python

- `tests/cognition/test_asr_session.py`
  - snapshot 返回一致的 state/session_id。
  - cancel 从 listening、speaking、processing 收敛到 idle。
  - cancel 不退出 tts。
  - cancel 后迟到的识别结果不再有效。
- `tests/cognition/test_pipeline.py`
  - start、status、cancel 复用同一 AsrSession。
- `tests/cognition/test_assembly.py`
  - 三个固定 voice service 已注册并正确分派。
- `tests/bus_server/test_gateway.py`
  - 三个 REST 接口调用正确 service。
  - unavailable 与 timeout 映射为稳定错误 envelope。
  - STT disabled 时返回不可用状态。

### 9.2 TypeScript / React

- `frontend/tests/voiceSlice.test.ts`
  - 状态、pending 和 error 转换。
- `frontend/tests/useVoiceControl.integration.test.tsx`
  - mount 时读取状态。
  - active 和 tts 状态按 `VOICE_POLL_MS` 轮询，idle 后停止。
  - 快捷键触发 start，再次触发 cancel。
  - repeat keydown 被忽略，unmount 清理 listener/timer。
  - 失败时保留服务端真值并展示错误。
- `frontend/tests/ChatInput.test.tsx`
  - 按钮状态、tooltip、aria 属性及点击行为。
  - 聊天通道断开时语音按钮仍按 voice availability 决定是否可用。

### 9.3 验证命令

```text
pytest tests/cognition/test_asr_session.py tests/cognition/test_pipeline.py \
  tests/cognition/test_assembly.py tests/bus_server/test_gateway.py
cd frontend && npm test -- --run
cd frontend && npm run build
pytest
```

Windows 手工冒烟：启动 supervisor 与桌面前端，分别用按钮和 `Ctrl+Shift+Space` 开始语音，
确认 UI 状态经过 listening/speaking/processing/idle，且 Yuki 最终通过 TTS 回复；在 processing
阶段取消，确认没有迟到回复。

## 10. 风险与已知限制

- **轮询负载**：每个活跃桌面窗口在 listening/speaking/processing/tts 期间每
  `VOICE_POLL_MS` 发起一次 GET；默认约为每秒 2 次。本次用户规模和 localhost 路径可接受，
  多窗口或远程部署若扩大需改为 WebSocket push。
- **窗口前台依赖**：`Ctrl+Shift+Space` 仅在 Yuki 窗口获得键盘事件时生效；系统级全局热键
  需要 Tauri 插件、权限与冲突处理，不属于本次范围。
- **最多一个轮询周期的 UI 延迟**：状态真实来源在 cognition，前端最多延迟
  `VOICE_POLL_MS` 才显示 speaking、processing、tts 或 idle。
- **取消不终止底层推理计算**：processing 时 cancel 会立刻令 session 失效并阻止结果发布，
  但已经提交的 STT worker 仍可能完成计算。
- **进程级麦克风依赖**：前端不直接申请麦克风权限；perception 进程未运行、OS 拒绝麦克风、
  音频设备故障或 `stt.enabled=false` 时，按钮只能显示不可用，不能在 WebView 内降级录音。
- **外部入口共享会话**：唤醒词、InteractionAgent trigger 和前端共用同一个 `AsrSession`；
  前端看到的是进程级真实状态，可能显示由其他入口启动的监听。
- **不镜像聊天记录**：本次语音转写和回复不会进入 `/ws/chat` 消息列表，只通过既有 TTS 链路
  交互；跨模态统一历史属于后续需求。
- **本地管理面安全**：三个 REST 端点必须继续受 localhost/CORS 限制，并保持固定 action，
  不暴露任意 bus service 或 topic。

## 11. 流程附加：代码审查阶段

实现和验证完成后，单独执行代码审查，检查：

- `finish()` 后回 listening 的续听窗口是否被保持，是否错误地提前收敛到 idle。
- active 与 tts 轮询是否都能停止于 idle，timer、listener 和迟到 promise 是否在卸载时清理。
- start/cancel 幂等、按钮/快捷键竞态和 session generation 是否阻止迟到 STT 发布。
- TTS ducking 是否可能被前端 start/cancel 意外打断。
- Gateway 是否使用类型化错误映射，是否越权暴露内部 bus 能力。
- UI 快捷键冲突、可访问名称、`aria-pressed`、禁用原因和聊天输入回归。
- 测试是否覆盖用户可见行为及失败路径，而非只覆盖内部方法。

审查发现的问题先修正并运行受影响测试；修正完成后再重复审查关键路径，最后才向用户交付。

## 12. 验收标准

- 用户可通过按钮或 `Ctrl+Shift+Space` 开始一次现有后端语音交互。
- 两个入口行为一致，重复触发不会创建并发 ASR 会话。
- 用户可取消 listening/speaking/processing，取消后不发布迟到识别结果。
- UI 展示真实后端状态，刷新后可恢复，不依赖本地倒计时猜测。
- TTS 期间不会误启动 ASR，前端会持续观察到 tts→idle，文字发送行为不受影响。
- 语音能力不可用时按钮禁用且给出原因。
- Python 与前端新增测试通过，前端构建通过；完整测试不引入回归。

## 13. 待用户确认

进入代码阶段前，请确认以下产品选择：

1. “语言功能”确实指“语音功能”。
2. 快捷键采用仅在 Yuki 窗口前台有效的 `Ctrl+Shift+Space`，本次不做系统级全局热键。
3. 再次点击按钮或再次按快捷键表示取消当前监听。
4. 本次只控制语音链路和展示状态，不把语音转写及回复同步到文字聊天记录。
