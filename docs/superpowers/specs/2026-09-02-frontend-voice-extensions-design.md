# Design: 桌面语音增强——系统级全局热键与语音轮次镜像

Date: 2026-09-02（修订版：落实首轮审查意见）

关联文档：

- [2026-09-02-frontend-voice-control-design.md](2026-09-02-frontend-voice-control-design.md)（基础文档；本设计取代其 §2.2 第 3/5 项与 §13 待确认 2/4 的排除决定，见 §1.1）
- [2026-08-30-yuki-desktop-frontend-design.md](2026-08-30-yuki-desktop-frontend-design.md)（桌面壳约束：前端不引入 Tauri JS API、capabilities 最小）
- [2026-08-23-asr-fullchain-design.md](2026-08-23-asr-fullchain-design.md)

## 1. 任务分析

### 1.1 需求解释（用户已确认的产品选择）

1. **系统级全局热键**：`Ctrl+Shift+Space` 在 Yuki 窗口失焦时也能触发/取消语音监听。
   基础文档 §2.2 第 3 项（"不实现系统级全局快捷键"）与 §13 待确认第 2 项（"本次不做系统级全局热键"）被本设计取代。
2. **语音转写与回复镜像进文字聊天记录**：识别成功的用户转写与 Yuki 回复作为普通消息进入聊天面板，与文字聊天同一条对话流并完整持久化（刷新/重启后仍可见）；取消/识别失败不产生占位气泡。
   基础文档 §2.2 第 5 项（"不把语音识别文本和语音回复镜像进聊天消息列表"）与 §13 待确认第 4 项被本设计取代。
3. 基础文档 §2.2 其余排除项（第 1/2/4/6 项）与 §13 待确认第 1/3 项**不变**。

### 1.2 用户目标

- 窗口失焦时按 `Ctrl+Shift+Space` 开始/取消监听；回到窗口后聊天面板能看到刚才的语音对话。
- 语音转写（用户气泡）与 Yuki 回复（助手气泡）实时出现在聊天面板，与文字消息同列表、同型、同历史。
- 全局热键被其他应用占用时，自动回退窗口内热键，并在 UI 明确提示失败原因。
- 浏览器开发模式（无 Tauri）行为不变：窗口内热键照常工作。

### 1.3 现状与缺口（勘察结论，行号级证据）

- **语音轮次已与文字同库持久化**：语音（`pipeline._recognize_utterance` → `Topics.USER_UTTERANCE` → `hub.on_user_utterance`）与文字（`hub.handle_chat_request`）都进入 `hub._handle_locked` 的 `context_wrapper.add_user/add_agent`（`src/yuki/cognition/brain/hub.py:210-212,254-263`），写入同一个 `ThreadTurnStore`（`thread_id=1`；`source` 标签同为 `user_input`/`agent_reply`，`store.py:82,225-247,266-323`）。**写入层零改动**；前端看不到只是因为缺少事件推送与历史读取。
- **`REPLY` 是语音专用信号**：文字路径 `publish_reply=False`（`hub.py:148-153`），语音路径 `publish_reply=True`（`hub.py:155-156`）；`if spoke and publish_reply:` 才发布 `REPLY`（`hub.py:270-274`）。`REPLY` 有 `kind: transition|final|cancel` 三型，镜像只应取 `final`。
- **hub 层持有配对信息**：`WorkingContext.add_user/add_agent` 返回 turn id（`working.py:19-23,42`）——语音用户轮次与回复轮次的关联（`reply_to_turn_id`）在 hub 内已知，是现成的事件源。
- **`/ws/chat` 是纯请求/响应通道**：`WsChannelSpec` 的 chat 通道只有 `message_handler`，无 `queue_factory`（`gateway.py:464-468`）；且 `create_ws_handler` 在 `message_handler` 非空时只 `await websocket.receive_json()`、**不消费 updates 队列**（`ws_channels.py:71-78`）——单纯给 chat 通道加 `queue_factory` 不会让推送到达客户端，必须改造 handler 支持"读消息 + 推队列"并存。
- **`/ws/status`、`/ws/perception` 的推送模式**：`queue_factory` + `initial_message` + `_broadcast`（`gateway.py:257-272,456-475`），可复用其队列与广播机制。
- **gateway 与 cognition 同 `local_bus`**（`app/main.py:94,142`），gateway 当前只订阅 `HEARTBEAT/FOCUS_CHANGED/SITUATION_UPDATE`（`gateway.py:196-198`），加订阅即可。
- **没有历史端点**：`GET /api/history/*` 不存在（`tests/bus_server/test_gateway.py:365-370` 显式断言 404）；前端重启后聊天面板为空——文字轮次同样不恢复。`ThreadTurnStore` 读 API 已具备（`items()`/`projection_items()`，`store.py:330-371`）。
- **无全局热键能力**：`src-tauri/Cargo.toml` 无任何 plugin；`capabilities/default.json` 仅 `core:default`；Rust 侧仅有 `probe_gateway()` 手写 TcpStream `GET /api/health`（`lib.rs:38-80`）与后端所有权/关闭逻辑。前端无 Tauri runtime API 依赖（`package.json` 仅有 `@tauri-apps/cli` 构建工具，非 runtime API）。
- **语音 REST 无 toggle 端点**：`frontend/src/api/rest.ts:44-49` POST/DELETE `/api/voice/listen`，前端按快照 `active` 二选一。
- **现有消息模型无 turn id**：`ChatMessage = {id(前端 UUID), role, text, createdAt, ...}`（`chatSlice.ts:4-12,30-32`），与后端 turn id 无关联——去重/历史映射需扩展字段。

### 1.4 术语边界

本设计引入的运行时词汇均为前端-网关-语音控制链路的工程状态，**不扩展** `CONTEXT.md` 领域词汇：

- `voice_turn`：hub 在语音路径发布的对话事件（`kind=user|reply`，携带后端 `turn_id`/`reply_to_turn_id`），用于聊天面板镜像。
- `toggle`：网关为全局热键提供的"active 则取消、否则开始"的原子切换语义（内部为读-判-写，安全依赖 start/cancel 幂等）。
- `hotkey`：全局热键注册状态（`{registered, error}`），由 Rust 上报、网关内存存储、随 `GET /api/voice` 下发。
- `history turns`：`ThreadTurnStore` 中的持久化轮次（`user_input`/`agent_reply`），经历史端点读取，供前端启动加载。

## 2. 不在范围内

- 用户可配置热键、多套热键绑定（固定 `Ctrl+Shift+Space`）。
- 修改文字聊天请求/响应路径（`/ws/chat` 文字流程的请求语义不变，仅增加推送能力）。
- 多会话/多线程（`thread_id=1` 单线程约束不变）。
- 镜像 `proactive` 轮次与 `REPLY` 的 `transition`/`cancel` 型（proactive 是无用户输入的开场，不入聊天面板）。
- `getUserMedia`/音频上传、替换 ASR/TTS、打包 Python、编辑 `config.yaml`。
- 引入 `tauri-plugin-global-shortcut` 之外的插件面；语音气泡特殊视觉样式（与文字完全同型）。

## 3. 核心决策

1. **全局热键注册与处理全部在 Rust 侧**，事件不经 WebView：`tauri-plugin-global-shortcut` 在 Rust 注册 `Ctrl+Shift+Space`，触发时由 Rust 直接 POST 网关 toggle 端点。保持基础文档"前端无 Tauri JS API、capabilities 保持 `core:default`"约束（Rust-only 插件 API 预期不经 capability 检查——列为 §8 实测项 1）。
2. **网关新增 `POST /api/voice/toggle`**：读当前快照，`active`（listening/speaking/processing）则 cancel、否则 start；`tts` 状态下 start 为 no-op（`AsrSession.begin()` 非 idle 返回空），toggle 不打断 TTS。语义为读-判-写，正确性依赖 start/cancel 幂等（`begin()` 非 idle 返回 `[]`、`return_to_idle` 幂等，`asr_session.py:60-73,145-154`），不承诺跨调用的"严格原子"。Rust 只调 toggle，避免重复实现读-判-写。
3. **热键注册状态回传 UI**：Rust 启动时注册并向网关上报（`POST /api/voice/hotkey {registered, error?}`，网关内存存储），`GET /api/voice` 快照增加 `hotkey: {registered, error}`。前端见 `registered=true` 时**跳过自身窗口级 keydown**；`registered=false` 时窗口级 keydown 自动成为兜底（浏览器 dev 无该字段 → keydown 生效）。此机制使"Win32 全局热键是否吞掉 WebView keydown"（§8 实测项 3）无论结果如何都只有一条路径生效。
4. **语音轮次事件由 hub 在已知配对点发布**：hub 在语音路径（`publish_reply=True`）发布新 topic `Topics.VOICE_TURN`，两种 kind：`kind="user"`（`add_user` 后，携带 `{turn_id, text, ts}`）与 `kind="reply"`（`add_agent` 后，携带 `{turn_id, reply_to_turn_id, text, ts}`）。文字路径不发布。不直接订阅 `USER_UTTERANCE`/`REPLY`：二者无配对 id 且 `REPLY` 需过滤非 final 型。
5. **镜像推送走改造后的 chat 通道**：`create_ws_handler` 支持 `message_handler` 与 `queue_factory` 并存（每个连接派生写任务消费 updates 队列，请求-响应语义不变）；gateway 订阅 `VOICE_TURN` 并 `_broadcast` 到 chat 通道队列。复用现有 `/ws/chat` 连接，前端无需新增连接。
6. **历史端点 + 前端启动加载**：cognition 注册服务 `cognition.history.turns`（读 `ThreadTurnStore.items()`，过滤 `source='proactive'`，按 id 倒序取最近 N=50），gateway 暴露 `GET /api/history/turns?limit=50`；前端 mount 时加载并入 `chatSlice`，live 事件与历史按 `turn_id` 去重。语音/文字同库同表，天然一致，同时修复"重启后聊天空白"的既有缺口。
7. **镜像不改文字链路**：语音气泡经 `voice_turn` 事件追加，不置 `pending`/`sendLocked`，不经过 `beginRequest`；`assistant_chunk` 路径不变。
8. **外部触发后的 UI 同步**：内容到达靠 `voice_turn` 推送；按钮状态靠 `window focus`/`visibilitychange` 时刷新 `GET /api/voice`（不在空闲期引入常驻轮询，维持基础文档"短轮询仅活跃期"）。

## 4. 后端设计

### 4.1 cognition：`voice_turn` 事件

- `topics.py` 新增 `VOICE_TURN = "event/voice_turn"`（沿用 `event/*` 前缀；`REPLY`（`event/reply`）同为 hub 发布、`AWAKE`（`event/awake`）由 `wake_word.py:167` 发布——本事件由 hub 在语音路径发布，与 `REPLY` 归属一致）。
- `hub._handle_locked` 语音路径（`publish_reply=True`）：
  - `user_turn_id = add_user(text)` 后发布 `kind="user"`，载荷 `{turn_id: user_turn_id, text, ts}`；
  - `agent_turn_id = add_agent(rendered, reply_to_turn_id=user_turn_id)` 后发布 `kind="reply"`，载荷 `{turn_id: agent_turn_id, reply_to_turn_id: user_turn_id, text: rendered, ts}`。
- 判定方式：`_handle_locked` 内 `publish_reply` 参数在调用链上可见（`hub.py:208,237` 已透传），语音路径是当前唯一 `publish_reply=True` 的入口；发布逻辑以该参数为条件，文字/唤醒路径不发布。
- 语音无回复（打断/失败，无 agent turn）时只有 `kind="user"` 事件——前端自然显示"只有用户气泡"，无占位（符合用户选择）。
- 事件发布复用现有 `self._bus.publish`（hub 已发布 `REPLY` 的同线程路径，无新锁要求）。

### 4.2 gateway：chat 通道推送改造与 `voice_turn` 广播

- **ws_channels.py 改造（共享基础设施，加法语义）**：`create_ws_handler` 当前在 `message_handler` 非空时只读 `receive_json()`（`ws_channels.py:71-78`）。改造为：当 `queue_factory` 与 `message_handler` 并存时，每连接派生一个写任务消费 updates 队列并向 socket 发送（复用现有纯推送通道的队列消费实现），读循环保持现有请求-响应语义不变；纯请求或纯推送通道行为不变。相关通道单测补充并存模式用例。
- **订阅与广播**：`GatewayRuntime` 增加 `bus.subscribe(Topics.VOICE_TURN, on_voice_turn)`；chat 通道定义增加 `queue_factory`（注册/注销队列复用 `register_perception_queue` 同型实现）；`on_voice_turn` → `_broadcast(chat_queues, ...)`（`gateway.py:257-272` 现有机制）。广播消息：

  ```json
  {"type": "voice_turn", "data": {"kind": "user", "turn_id": 12, "reply_to_turn_id": null, "text": "...", "ts": 1725...}}
  ```

  推送类消息采用 `{type, data}` 包裹，与 `/ws/status` 的 `{type:"health", data}`、`/ws/perception` 的 `{type:"text_extract", data}` 线格式一致；chat 通道现有的请求响应消息（`assistant_chunk` 等顶层字段）不变，前端按 `type` 分发。

### 4.3 gateway：新端点

- `POST /api/voice/toggle`：转发 cognition voice 服务——cognition 侧新增 `cognition.voice.toggle` 服务（与 `cognition.voice.start/cancel/status` 同模式，`assembly.py` 注册），内部读快照 → 按 active 调 start/cancel（读-判-写；`tts` 时 no-op）；gateway 只转发并返回状态快照。错误映射同基础文档 §5.3（503/504/500 envelope）。
- `POST /api/voice/hotkey`：请求 `{registered: bool, error?: str}`；gateway 内存存储（字段挂 `GatewayRuntime`），`GET /api/voice` 响应由 gateway 侧包装附加 `hotkey` 字段（voice 服务快照 + gateway 持有的 hotkey 状态合并，不改 cognition 快照契约）。Rust 每次启动重报；网关重启后 `hotkey: null`，前端视为未知、keydown 兜底生效（行为安全）。
- `GET /api/history/turns?limit=50`：`bus.request(COGNITION_HISTORY_TURNS_SERVICE, {limit})` → 返回 `{turns: [{id, role, source, content, ts}]}`（按 id 倒序；`source='proactive'` 已过滤）；错误映射：服务不可达 `503 history_unavailable`、超时 `504`、非法参数 `422`。cognition 侧服务注册与现有 `bus.respond(COGNITION_AWAKE_SERVICE, hub.handle_awake_request)` 同模式（`assembly.py:353`）；hub 暴露 `history_turns(limit)` 方法，委托其 context store 的 `items()`（`store.py:330-335`）过滤后返回。

### 4.4 src-tauri（Rust）

- `Cargo.toml` 增加 `tauri-plugin-global-shortcut = "2"`（版本与 `Cargo.lock` 锁定的 tauri 2.11.5 兼容性列为 §8 实测项 1）；`tauri.conf.json` 是否需要 `app.plugins` 声明同列实测项。
- `lib.rs` setup 中 `.plugin(tauri_plugin_global_shortcut::init())` 并注册 `Ctrl+Shift+Space`（accelerator `"ctrl+shift+space"`）：
  - 成功 → `POST /api/voice/hotkey {"registered": true}`；
  - 失败（占用/系统拒绝）→ `POST ... {"registered": false, "error": "<err>"}`，不阻止应用启动；
  - 触发回调 → `POST http://127.0.0.1:8765/api/voice/toggle`。
- Rust 侧新增小型 HTTP POST helper（扩展现有 TcpStream 手写 HTTP 模式，`lib.rs:38-80`，1s 超时；gateway 不可达时静默丢弃——语音控制本就依赖后端存活）。
- capabilities 不变（`core:default`）；若实测要求 permission，只补最小 `global-shortcut` 项（仍不经前端 JS 暴露）。
- 浏览器 `npm run dev`（无 Tauri）：无全局热键，前端 keydown 兜底（`hotkey` 字段缺失 → 视为未注册）。

## 5. 前端设计

### 5.1 类型与消息模型

- `types/api.ts`：`VoiceStatus` 增加 `hotkey: {registered: boolean; error: string} | null`；新增 `HistoryTurn {id, role, source, content, ts}`；`/ws/chat` 消息联合增加 `voice_turn`（`{type:"voice_turn", data:{kind, turn_id, reply_to_turn_id, text, ts}}`）。
- `chatSlice` 消息模型扩展：`ChatMessage` 增加可选 `turnId?: number`（后端 turn id；文字消息与旧消息无此字段）。历史与 live 事件的去重键：`turnId` 存在时按 `turnId`，否则按本地 `id`。
- `chatSlice` 新增动作：
  - `appendVoiceUserTurn(turn_id, text, ts)` / `appendVoiceReplyTurn(turn_id, reply_to_turn_id, text, ts)`：按 `turnId` 幂等去重后追加同型气泡；**不**置 `pending/sendLocked`、不触碰 `requestGeneration`。
  - `hydrateHistory(turns)`：mount 时调用；turns 为 id 倒序，渲染前反转为时间正序，批量替换 `messages`（现有未持久化消息语义：pending 请求进行中时不 hydrate 或合并——设计取：pending 时不 hydrate，待 `receiveAssistant` 完成后再加载，避免覆盖进行中的对话）。
- `useChat`：消息分发增加 `voice_turn` 分支（现有分发只处理 `assistant_chunk`，`useChat.ts:13-19`）→ 调对应 action。
- `useVoiceControl`：
  - mount 时先 `GET /api/history/turns`（失败静默，不阻塞语音功能）；
  - keydown 注册条件改为 `hotkey?.registered !== true` 才挂窗口级监听（注册失败/浏览器 dev 时生效），卸载清理不变；
  - `window focus` + `visibilitychange→visible` 时刷新 `GET /api/voice`；
  - 按钮点击逻辑不变（POST/DELETE 按 active），与 Rust toggle 并存——两路径交错的结果仍是幂等的 start/cancel（§3 决策 2 的安全前提），可接受。

### 5.2 组件

- 语音状态提示（tooltip/提示行）三态：
  - `hotkey.registered === true`："全局热键已启用（Ctrl+Shift+Space）"；
  - `hotkey.registered === false`："全局热键不可用（被占用），已回退窗口内快捷键"；
  - `null`（未知/浏览器 dev）：不显示额外提示。
- 聊天面板：语音气泡与文字气泡完全同型（`MessageBubble` 不变，无来源标记）。

## 6. 预计修改范围

后端（Python）：

- `src/yuki/topics.py`（`VOICE_TURN`）
- `src/yuki/cognition/brain/hub.py`（语音路径发布 voice_turn；`history_turns(limit)`）
- `src/yuki/cognition/assembly.py`（新增 `COGNITION_VOICE_TOGGLE_SERVICE` 常量与 handler——同现有 `cognition.voice.*` 模式，`assembly.py:71-73,115-131,387-389`；新增 `cognition.history.turns` 服务注册）
- `src/yuki/bus_server/ws_channels.py`（handler 并存模式改造）
- `src/yuki/bus_server/gateway.py`（订阅/广播、toggle/hotkey/history 端点、快照合并）
- `tests/cognition/test_hub.py`、`tests/cognition/test_assembly.py`
- `tests/bus_server/test_gateway.py`、`tests/bus_server/test_ws_channels.py`（新增并存模式用例）

前端（TypeScript/React）：

- `frontend/src/types/api.ts`
- `frontend/src/api/rest.ts`（toggle/hotkey/history client）
- `frontend/src/api/ws/types.ts`、`frontend/src/api/ws/channels.ts`（`voice_turn` 消息类型分发）
- `frontend/src/state/slices/chatSlice.ts`（`turnId`、appendVoice*、hydrateHistory）
- `frontend/src/state/hooks/useChat.ts`（voice_turn 分支、历史加载）
- `frontend/src/state/hooks/useVoiceControl.ts`（keydown 条件、focus/visibility 刷新）
- `frontend/src/components/chat/ChatInput.tsx`（hotkey 提示三态）
- `frontend/tests/chatSlice.test.ts`、`frontend/tests/useChat.test.ts`、`frontend/tests/useVoiceControl.integration.test.tsx`、`frontend/tests/ChatInput.test.tsx`

Rust：

- `frontend/src-tauri/Cargo.toml`（plugin 依赖）
- `frontend/src-tauri/tauri.conf.json`（按实测项 1 结论，可能需要 `app.plugins`）
- `frontend/src-tauri/src/lib.rs`（插件初始化、注册/上报/触发回调、HTTP POST helper）

## 7. 测试策略

按 TDD，测试先于对应代码。

### 7.1 Python

- hub：语音路径发 `kind=user`+`kind=reply` 两事件且 `turn_id`/`reply_to_turn_id` 正确；文字路径不发；打断（无回复）只有 user 事件；唤醒路径不发；`history_turns` 过滤 proactive、倒序、limit。
- gateway：`voice_turn` 订阅 → chat 队列广播；`POST /api/voice/toggle` 转发与 503/504 映射；`POST /api/voice/hotkey` 存取 + `GET /api/voice` 快照合并（cognition 快照字段不被污染）；`GET /api/history/turns`（proactive 过滤、limit、503/504）。
- ws_channels：并存模式——有 `message_handler`+`queue_factory` 的通道既能处理请求又能收到推送；纯请求/纯推送通道行为不回归。

### 7.2 TypeScript/React

- chatSlice：appendVoice* 追加/幂等去重（与历史重叠不重复）；hydrateHistory 正序渲染、pending 时跳过；`turnId` 字段。
- useChat/useVoiceControl：`voice_turn` 分发；keydown 在 `hotkey.registered=true` 时不注册、false/null 时注册；focus/visibility 刷新；卸载清理。
- ChatInput：hotkey 提示三态文案。
- integration：历史加载 → live voice_turn 到达 → 无重复气泡；外部 toggle 后 focus 刷新按钮状态。

### 7.3 验证命令与 Windows 冒烟

```text
pytest tests/cognition/test_hub.py tests/bus_server/test_gateway.py tests/bus_server/test_ws_channels.py
cd frontend && npm test -- --run && npm run build
pytest
```

Windows 冒烟（e2e，不可省略，在 §8 第 7 步执行）：

- 注册成功路径：release/debug 包启动 → 窗口失焦按 `Ctrl+Shift+Space` → 开始监听 → 再按取消 → 回窗口看聊天面板出现转写与回复气泡；重启应用（重新加载历史）轮次仍在。
- 注册失败路径：先用其他应用占用 `Ctrl+Shift+Space` → 启动 Yuki → UI 显示"全局热键不可用" → 窗口内按键仍可开关（兜底生效）。
- 后台镜像：窗口失焦触发语音 → 等 Yuki 回复 → 回窗口气泡已在；双路径验证 `registered=true` 时前端 keydown 不触发（无 toggle 翻转）。

## 8. 实现顺序（每步可验证）

1. **实测项验证 + Rust 插件骨架**：`tauri-plugin-global-shortcut` 与 tauri 2.11.5 兼容、`app.plugins` 声明、Rust-only 是否绕过 capability、冲突 Err 形态、Win32 吞键行为（5 项实测结论写入 README）；注册、hotkey 上报、toggle POST helper；`cargo check` + 手动注册/冲突冒烟。
2. **cognition**：`VOICE_TURN` 事件 + hub 语音路径发布 + `history_turns`；单测（文字不发、id 正确、proactive 过滤）。
3. **ws_channels 并存模式**：改造 + 单测（请求与推送并存、纯通道不回归）。
4. **gateway**：订阅广播、`toggle`/`hotkey`/`history` 端点 + 单测。
5. **前端数据层**：类型、消息分发、chatSlice 追加/去重/hydrate + 单测与 integration。
6. **前端交互**：keydown 条件、focus 刷新、hotkey 提示 + 组件测试。
7. **集成与冒烟**：相关 pytest → 前端全量 → 完整 pytest → Windows 冒烟（§7.3）。

## 9. 风险与已知限制

- **热键占用**：`Ctrl+Shift+Space` 被其他应用占用时回退窗口内热键并 UI 提示；注册失败不阻塞启动。
- **gateway 内存态**：`hotkey` 状态不持久化——Rust 每次启动重报；gateway 重启窗口期前端视为未知（keydown 兜底，安全）。
- **镜像顺序与去重**：live 事件按 hub 发布序到达，`turn_id` 幂等去重；跨模态交错（语音回复 vs 文字回复）按 `ts` 排序可接受。历史加载与 live 事件的竞态由 slice 幂等去重（`turnId` 键）收敛。
- **不镜像 proactive/cancel/transition**：聊天面板只呈现完整语音问答；打断时只有用户气泡。
- **浏览器 dev 无全局热键**：预期行为（无 Tauri），keydown 兜底。
- **chat 通道推送与串行请求**：请求处理中（含 60s 超时的 `to_thread` 执行期）推送由独立写任务投递，不阻塞请求响应；推送不进入请求-响应状态机（不置 pending）。
- **Rust HTTP 依赖**：toggle/hotkey 上报沿用 TcpStream 手写 HTTP，无新 crate（除插件本体）；gateway 不可达时静默丢弃。
- **双路径双触发**：`registered=true` 跳过 keydown + start/cancel 幂等双重防护；按钮与 Rust toggle 并存的交错结果幂等可接受。
- **toggle 非严格原子**：读-判-写窗口内状态变化由幂等兜底，极端交错（如 processing 完成瞬间）以服务端快照为准，UI 以轮询/推送收敛。

## 10. 代码审查阶段

实现和验证完成后，交给独立 review agent 检查：

- hub 语音路径判定（`publish_reply=True`）是否可能漏发/多发 voice_turn；事件发布与 turn 写入的时序。
- ws_channels 并存模式改造的回归面（纯请求/纯推送通道行为不变）。
- toggle 读-判-写与前端按钮路径交错是否产生非预期会话状态。
- hotkey 上报/合并链路（Rust → gateway 内存 → 快照 → 前端）的失败与重启路径。
- history hydrate 与 live 去重的竞态、pending 状态下的加载行为。
- API 是否越权暴露内部能力；测试是否覆盖用户可见行为。
- 审查发现的问题先修正并回归测试，再向用户交付。

## 11. 验收标准

- 窗口失焦按 `Ctrl+Shift+Space` 可开始/取消监听；回窗口聊天面板出现该轮语音转写与 Yuki 回复气泡，与文字同列表、同型。
- 重启应用后历史仍可见（语音与文字轮次，同一对话流）。
- 热键被占用时 UI 明示回退，窗口内热键仍可用；浏览器 dev 行为不变。
- 文字聊天发送/接收路径零变化（请求语义不变）；基础文档 §10 语音控制验收不回归。
- Python/前端新增测试通过、完整测试不引入回归；Windows 冒烟（§7.3）通过。

## 12. 待用户确认

1. 历史端点与前端启动加载**同时恢复文字轮次**（修复"重启后聊天空白"的既有缺口）——推荐接受（语音/文字同库同表，不做区分更简单一致）。
2. 语音气泡与文字完全同型、无来源标记——推荐接受（同一条对话流）。
3. 确认本设计取代基础文档 §2.2 第 3/5 项与 §13 待确认 2/4，其余排除项不变——确认后即可进入编码阶段。
