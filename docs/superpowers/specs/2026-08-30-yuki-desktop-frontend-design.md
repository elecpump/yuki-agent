# Design: Yuki 桌面端前端 v1（Tauri v2 + React + TS + Vite）修订版

Date: 2026-08-30

关联文档：[2026-08-23-desktop-gateway-design.md](2026-08-23-desktop-gateway-design.md)（后端 Gateway v1/v2 范围）

## Goal

在项目根新建 `frontend/`，用 Tauri v2 + React + TypeScript + Vite 构建桌面应用。
v1 只交付**对话界面 + 健康面板**，其余控制台标签为 v2 占位。前端不引入任何 Tauri
JS API（纯 Web 技术栈：fetch + WebSocket），可在普通浏览器中独立开发调试。

## 用户决策（不变）

- 界面形态：对话优先 + 可折叠控制台面板（左 70% 对话 / 右 30% 抽屉）
- 窗口模式：v1 独立窗口（可缩放、最小化）；v2 置顶悬浮窗
- v1 范围：对话 + 健康/状态；Memory/Soul/Config/Perception 为 v2 占位

## 总体判断（相对原方案的修订）

原方案骨架成立（目录结构、组件树、Zustand 分层、10 步实现顺序均保留），但
"后端无需改动——Gateway 已提供全部所需接口"的前提**不成立**。除接口核验外，
本修订版同时收紧了取消、健康新鲜度、断线语义与桌面进程生命周期，避免前后端按
各自假设实现后无法联调：

1. **`assistant_chunk` 不含 `reason`**（`gateway.py:397-404`）：WS 回复只有
   `{type, task_id, text, done, status, error}`。`reason/ts/spoke/emotion` 只在
   REST `POST /api/chat` 的 `result` 里。→ v1 明确补齐 WS 合约（§决策 D1）。
2. **`/api/soul`、`/api/memory*` 未实现**：`gateway.py:473-507` 实际路由只有
   health / history / chat / config / perception/status。后端设计文档规划了
   memory/soul 端点，但落地时未包含。→ 属 v2 后端工作，v1 的 DTO 与 API 表不含。
3. **interrupt 在 v1 无法真正中止服务端**，两层原因：
   - 同一 WS 连接的消息处理是**串行**的（`ws_channels.py:71-78` + `gateway.py:386`
     `await asyncio.to_thread(run_chat)` 阻塞期间不读下一个消息），interrupt 会排在
     60s 聊天任务（`chat_task_timeout_s`）之后才被处理；
   - `chat/interrupt` 总线话题**无订阅者**（全仓库仅 `gateway.py:381` 一处 publish；
     hub 的中断机制是 utterance probe `hub.py:501-503`，与 interrupt 消息无关）。
   与后端设计文档"v1 仅记录，真正中止属 v2"一致。→ 前端做**本地取消**（§决策 D2）。
4. **chat 通道不能发 `{type:"ping"}`**：`_chat_message_handler` 对非 interrupt 消息
   取 `text or ""`，空文本会触发一次真实 `run_chat`。→ 心跳只发 `/ws/status`
   （该通道客户端消息被丢弃、仅更新 last_seen，安全；§决策 D3）。
5. **服务端 prune 不关闭 socket**（`gateway.py:128-151`）：超时只从注册表移除，
   websocket 与推送队列继续存活，客户端永远收不到 close 事件。→ 重连由"网络断 /
   服务器重启"触发；`useStatusPing` 语义改为"最近收到服务端消息的时效"（§决策 D4）。
6. **健康拓扑与"进程×组件"表不符**：心跳按 OS 进程发布（`yuki` 主进程 +
   `model_worker`），组件名带限定前缀；vlm/stt/models/tts 在 model_worker 侧
   （`app/main.py:192-202` 排除 + `model_worker/agent.py:72-83`）；"bus_server 的
   proxy/router"是 hub 内建 liveness（`bus.py:179-198`），出现在快照的 `hub` 字段。
   → HealthPanel 通用渲染，不硬编码分类表（§决策 D5）。
7. **CORS 缺 Windows 生产 origin**：默认 `cors_origins=["tauri://localhost"]` +
   regex `^http://localhost:\d+$` 只覆盖 dev；Tauri v2 在 Windows WebView2 的
   origin 是 `http://tauri.localhost`（macOS/Linux 才是 `tauri://localhost`）。
   不配置则生产包 fetch `/api/health` 直接 CORS 失败（WS 无预检不受影响）。
   → 修改默认值与示例配置，并给已有 config 增加迁移提示，见 §后端文件改动。
8. **健康推送 5s 是配置值**（`config.py:103` `heartbeat_interval_s=5.0`）：前端不
   硬编码 5s；连接状态使用分通道时间戳与 REST 探测，不把一次 chat 回复当成 status
   通道健康证明。
9. **pending 阶段拿不到服务端 `task_id`**：`task_id` 在 `run_chat()` 内创建，且只在
   阻塞 RPC 完成后的 `assistant_chunk` 中返回。→ v1 本地取消不能依赖 task_id，也不
   发送无效 interrupt；使用单 pending 下的本地 request generation（§决策 D2）。
10. **健康心跳 DTO 少于 `collect()` DTO，且没有过期语义**：实际广播只有
    `process/ts/healthy/components`，Gateway 又永久保留最后一帧。→ 前端只渲染真实
    字段；后端快照补 `fresh/last_seen_age_s`（§决策 D5）。
11. **WS 增量快照中的 hub 不是实时值**：连接初始帧查询 hub，随后心跳帧只携带
    `{healthy: null, cached: true}`。→ WS 负责低延迟进程更新，REST 每 30s 做权威
    全量刷新（§决策 D5）。
12. **桌面生产启动不能依赖示例配置注释**：默认 Gateway 关闭，GUI 工作目录也不
    稳定。→ 明确外部后端/自启动后端两种所有权模式、解释器与工作目录解析、优雅
    关闭和 v1 Windows 冒烟测试（§Tauri 集成）。

## 目录结构（修订）

```
frontend/
├── package.json
├── tsconfig.json
├── vite.config.ts                  # host: "localhost", strictPort: true
├── index.html
├── src/
│   ├── main.tsx                    # React 入口
│   ├── App.tsx                     # 布局壳: ChatPanel + ConsoleDrawer
│   ├── styles/
│   │   ├── theme.ts                # Ant Design 暗色主题
│   │   └── global.css
│   ├── api/
│   │   ├── client.ts               # fetch 封装: base URL, 错误归一化, 错误信封解析
│   │   ├── rest.ts                 # v1: getHealth(); 其余端点 stub
│   │   └── ws/
│   │       ├── ConnectionManager.ts# 多通道 WS, 重连, 心跳（仅 status 通道）
│   │       ├── channels.ts         # 通道消息分发
│   │       └── types.ts            # WS 消息类型联合（对齐 §API 实际面）
│   ├── state/
│   │   ├── store.ts                # Zustand 根 store
│   │   ├── slices/
│   │   │   ├── chatSlice.ts        # 消息列表, pending, requestGeneration, ignoreGeneration
│   │   │   ├── healthSlice.ts      # processes, hub, gateway, 分通道连接与 REST 探测状态
│   │   │   └── uiSlice.ts          # 控制台开关, 当前标签
│   │   └── hooks/
│   │       ├── useChat.ts          # /ws/chat 收发（本地 generation 取消，不自动重发）
│   │       ├── useHealthStream.ts  # /ws/status 订阅 + REST 定期权威刷新/故障探测
│   │       └── useStatusPing.ts    # status 通道保活与消息时效检测（见决策 D4）
│   ├── components/
│   │   ├── chat/
│   │   │   ├── ChatPanel.tsx       # 消息列表 + 输入
│   │   │   ├── MessageBubble.tsx   # 用户右 / yuki 左
│   │   │   ├── ReasonBadge.tsx     # 按决策 D1 的数据源
│   │   │   ├── ThinkingIndicator.tsx
│   │   │   └── ChatInput.tsx       # Enter 发送, Shift+Enter 换行, pending 时禁用发送
│   │   ├── console/
│   │   │   ├── ConsoleDrawer.tsx
│   │   │   ├── ConsoleTabs.tsx     # Health [启用], 其余 v2 占位
│   │   │   ├── health/
│   │   │   │   ├── HealthPanel.tsx # 通用进程列表 + hub 卡 + gateway 卡
│   │   │   │   ├── ProcessCard.tsx # 任意进程名, 绿/红/过期灰, 可展开
│   │   │   │   └── ComponentList.tsx # 任意组件名 + {ok, detail} 键值渲染
│   │   │   └── Placeholder.tsx     # "v2 即将上线"
│   │   └── shell/
│   │       └── ConnectionStatus.tsx # 底栏: 按决策 D4 分通道四态
│   ├── config/
│   │   └── runtime.ts              # BASE_URL (默认 http://127.0.0.1:8765, VITE_API_BASE_URL 覆盖)
│   └── types/
│       └── api.ts                  # DTO 类型（对齐 §API 实际面）
├── src-tauri/
│   ├── Cargo.toml                  # tauri-build build-dependencies（见 §Tauri 集成）
│   ├── build.rs                    # tauri_build::build()
│   ├── tauri.conf.json             # v2 schema（见 §Tauri 集成）
│   ├── capabilities/default.json   # core:default（v1 前端无 Tauri JS API）
│   ├── icons/                      # npm run tauri icon 生成，勿留空
│   └── src/
│       ├── main.rs
│       └── lib.rs                  # 后端探测、所有权、启动与分级关闭
└── tests/                          # 与 src 模块同级的 *.test.ts(x)
```

## 组件树

```
App
├── ChatPanel (左侧主区域, 70%)
│   ├── MessageList (可滚动, 新消息滚动到底)
│   │   └── MessageBubble[]
│   │       └── ReasonBadge (数据源见决策 D1)
│   ├── ThinkingIndicator (等待回复时)
│   │   └── InterruptButton (纯客户端取消, 见决策 D2)
│   └── ChatInput (Enter 发送 / Shift+Enter 换行 / pending 时禁用发送)
└── ConsoleDrawer (右侧, 30%, 可折叠)
    ├── ConsoleTabs
    │   ├── Health [启用]  → HealthPanel
    │   ├── Memory [禁用]  → Placeholder (v2: 需后端新增 /api/memory*)
    │   ├── Soul [禁用]    → Placeholder (v2: 需后端新增 /api/soul)
    │   ├── Config [禁用]  → Placeholder (v2: GET /api/config 已存在, 可提前)
    │   └── Perception [禁用] → Placeholder (v2: /ws/perception 已存在)
    └── ConnectionStatus (底栏: 分通道四态, 见决策 D4)
```

## API 实际面（已核验，非规划）

### REST 端点（`gateway.py` 实际注册）

| Method | Path | Response | 备注 |
|--------|------|----------|------|
| `POST` | `/api/chat` | `{task_id, status, created_at, result: {text, ts, emotion, spoke, reason} \| null, error}` | 同步阻塞 ≤60s，仅供诊断；UI 不自动切换 |
| `GET` | `/api/chat/{task_id}` | task dict（同上） | 404 走错误信封 |
| `GET` | `/api/health` | `{gateway: {healthy, process, started, ts}, hub, processes}` | hub 为 1s 超时 RPC，失败时 `{healthy: false, error}` |
| `GET` | `/api/config` | 脱敏全量 config | 已有，v1 不用 |
| `GET` | `/api/perception/status` | `{degraded, components, heartbeat}` | 已有，v1 不用 |
| `GET` | `/api/history/sessions` / `/api/history/{id}` | 会话列表 / turns | 已有，v1 不用 |
| ~~`GET` `/api/soul`~~ | — | **未实现** | v2 后端工作 |
| ~~`GET/DELETE` `/api/memory*`~~ | — | **未实现** | v2 后端工作 |

业务 REST 错误通常使用错误信封：
`{"error": {"code": str, "message": str, "details": {}}}`。

不能假设所有失败都符合该结构：当前 HTTP handler 还可能给出 `http_error`，FastAPI
请求校验失败会返回 422 `{"detail": [...]}`，代理/网络错误也可能不是 JSON。
`api/client.ts` 必须把 `code` 当开放字符串，并兼容错误信封、FastAPI detail、纯文本
响应和 fetch 网络异常，统一归一化为前端 `ApiError`。

v1 对话 UI 只使用 `/ws/chat`。WS 在请求过程中断开时不能确定服务端是否已经执行，
因此不得自动改发 REST 或重发 WS；按 D2 显示不可恢复错误，由用户显式决定是否重试。

### WebSocket 通道

**`/ws/status`**（连接即收初始快照，之后随心跳推送，默认每 5s）
- Server→Client: `{type: "health", data: {gateway, hub, processes}}`
- Server→Client: `{type: "ping", channel, ts}`（每 30s，仅对未超时连接）
- 客户端消息：内容被丢弃、仅更新 last_seen（可安全用作保活）

**`/ws/chat`**（无初始消息；**串行**：一个请求完成后才读下一条）
- Client→Server: `{text, session_id}` 或 `{type: "interrupt", task_id}`
- Server→Client: `{type: "assistant_chunk", task_id, text, done, status, error}`
  （done 恒为 true，非流式；**无 reason/ts/spoke/emotion**）
- Server→Client: `{type: "interrupt_ack", task}`（仅状态登记，不中止服务端）
- ⚠️ 不要在此通道发送 `{type:"ping"}`（会触发空文本聊天）
- ⚠️ 发出 chat 后、收到 `assistant_chunk` 前，前端不知道服务端 `task_id`；v1 UI 不
  使用 interrupt 消息（协议保留给后续后端改造）。

**`/ws/perception`**（v2；snapshot / foreground / text_extract 三型已实现）

### Chat Reason 码（真实集合，`hub.py` 的 `_result` 调用点）

```
crisis | cloud | chat_local | chat_local_failed | chat_local_empty
| interrupted | l2_unavailable_fallback | silent
```

> 原方案中的 `tool_local`、`vision_cloud` 全仓库不存在；`situation` 不是 chat 路径
> 的 reason（那是 proactive 的 L2 自由文本）；`l2_unavailable_fallback` 不重复。

### 健康拓扑（真实结构）

心跳按 **OS 进程**发布（默认 5s 一次，`config.health.heartbeat_interval_s`）：

| 进程 | 组件（组件名即 key） |
|------|---------------------|
| `yuki`（主进程） | `bus_hub`, `local_runtime_bus`, `remote_bus`, `compatibility_bridge`, `cognition.brain`(installed), `cognition.l2`(enabled/degraded/installed/configured/api_key_present), `cognition.pipeline`(frame_client_available), `cognition.memory`(db), `cognition.thread_maintenance`, `perception.audio`(stream_active), `perception.capture`(frame_registered), `perception.monitor`(thread_alive), `perception.scroll_hook`(installed), `perception.text`(enabled), `perception.wake_word`(enabled/failed), `interaction.hotkeys`(installed), `agent_loops` |
| `model_worker` | `manager`, `manager_loop`, `scheduler`, `operations`, `gpu_runtime`, `model.vlm`, `model.stt`, `model.local_chat`, `model.tts`(output: console), `model.embedding` |

快照字段（按 v1 后端补丁后的合约）：
- `processes`: `Record<进程名, {process, ts, healthy, fresh, last_seen_age_s, components}>`；
  `pid/uptime_s/error_count` 当前不在心跳广播中，不作为 v1 必需字段，未来出现时
  ProcessCard 以可选字段展示；
- `hub`: `{process: "bus_server", pid, uptime_s, error_count, healthy, components: {proxy: {ok, last_forwarded_s, last_heartbeat_s}, router: {ok}}}`（BusHub liveness，1s 超时 RPC，失败时 `{healthy: false, error}`）
- `gateway`: `{healthy, process: "gateway", started, ts}`

`/ws/status` 的初始快照含实时 hub；后续随进程心跳产生的增量快照中，hub 为
`{healthy: null, cached: true}`。前端不得用 cached hub 覆盖最后一次权威 REST/初始值。

## 设计决策（D1–D5）

### D1: ReasonBadge 数据源

- **确定采用后端补字段方案**：`gateway.py` 的 `_chat_message_handler` 补
  `reason/ts/spoke/emotion`（从 `result` 读取），并在
  `tests/bus_server/test_gateway.py` 增加 WS 合约单测。
- ReasonBadge 显示开放字符串 reason；未知 reason 原样展示，不在前端维护封闭枚举。
- `status=failed` 或 reason 缺失时，显示状态徽标，不阻塞消息正文渲染。

### D2: 纯客户端取消（v1）

服务端 v1 不中止，而且 pending 阶段前端拿不到服务端 `task_id`。v1 不发送
`{type:"interrupt"}`，只实现明确的本地 UI 取消：

1. 每次发送前递增本地 `requestGeneration`；由于 v1 只允许一个 pending，当前
   generation 唯一对应这次 WS 请求；
2. 用户点取消 → 将当前 generation 写入 `ignoreGeneration`，立即清 thinking、
   `pending=false`，关闭旧 `/ws/chat` 并开始重建；新 socket open 前保持输入框禁发；
3. socket generation 隔离旧连接，旧 handler 即使迟到也不能把 chunk 分发给新连接；
   新连接成功后清理 `ignoreGeneration` 并恢复发送；
4. 不允许在仍被服务端阻塞的旧 socket 上排队新请求；
   但新 socket 上立即发送的新请求仍可能在服务端等待：DecisionHub 的
   `_decision_lock` 覆盖完整决策/cloud call，旧请求未释放锁前，第二个 chat 会被
   串行化。UX 必须把这视为预期排队，继续显示 thinking，并容忍最长
   `chat_task_timeout_s`（默认 60s）的等待，不能在前端再次自动重试；
5. chat WS 在 pending 时断开：标记本次请求失败，提示“连接中断，回复可能已执行但
   结果无法恢复”，**不自动重发**，避免重复对话/记忆写入；
6. v2 升级为服务端真中止时，协议必须先提供 client request id、立即返回
   `task_started`，或接受客户端生成的 task_id，并使用独立控制连接处理 interrupt。

### D3: 心跳策略（分通道）

- `/ws/status`：客户端每 25s 发 `{type:"ping"}`（内容被服务端忽略，仅保活，留在
  ping 集合内持续收到服务端 ping 作为活性信号）；
- `/ws/chat`：**不发送任何心跳**。空闲被 prune 无害（socket 不断、后续消息仍被
  处理）；重连由网络断/服务重启触发，pending 断线遵循 D2，不自动重发；
- `/ws/perception`：v1 不连接。

### D4: ConnectionStatus 分通道四态

不得用一个 `wsConnected/lastMessageAt` 混合 status 与 chat。分别维护
`statusWsState/statusLastMessageAt`、`chatWsState/chatLastMessageAt`，以及
`restReachable/lastRestSuccessAt`。REST `/api/health` 每 30s 做一次权威全量刷新；WS
断开或 75s 未收到 status 服务端消息时立即额外探测一次（75s 覆盖默认 30s server
ping 与调度抖动，不依赖固定 5s 进程心跳）。

- **正常**：status WS 正常收到消息，且最近一次 REST 成功；
- **状态流异常**：REST 成功，但 status WS 已断开或超过 75s 无服务端消息；继续 REST
  刷新并后台重连 WS；
- **对话重连中**：Gateway/REST 可达，但 chat WS 断开；禁用发送，指数退避重连
  （上限 10s）；
- **后端不可达**：status/chat WS 均不可用，且最近一次 REST 探测失败。

server ping、health 都只更新 status 时间戳；assistant_chunk 只更新 chat 时间戳，不能
证明 status 通道健康。客户端 status ping 只用于服务端保活，不算服务端可达证明。

### D5: HealthPanel 通用渲染

- Gateway `_health_snapshot()` 为每个进程计算
  `last_seen_age_s = now - heartbeat.ts`，以及
  `fresh = last_seen_age_s <= max(15s, 3 * heartbeat_interval_s)`；保留原始
  `healthy`，由前端组合显示；
- `ProcessCard` 渲染任意进程名：`healthy && fresh` 为绿、`!healthy && fresh` 为红、
  `!fresh` 为灰色“心跳过期”；展开显示 `last_seen_age_s`，并仅在实际存在时显示
  `uptime_s/error_count/pid`；
- `ComponentList` 渲染任意组件名 + `{ok, detail}`，detail 按键值对展示
  （loaded/degraded/installed/stream_active/...）；
- 快照的 `hub` 与 `gateway` 各一张独立卡片（hub 失败时显示"hub 不可达"，不视为
  进程崩溃）；
- WS 帧中的 `hub.cached === true` 时不覆盖权威 hub；连接初始帧与每 30s REST 结果可
  更新 hub。REST 同时全量校准 processes，修复 WS 队列丢帧造成的偏差；
- **不写死** perception/cognition/interaction/bus_server 分类表——后端演进（新增
  进程或组件）时前端零改动。

## 状态管理（Zustand，保留原方案）

- `chatSlice`: `{messages, pending, sendLocked, requestGeneration, ignoreGeneration, error}` +
  `beginRequest` / `receiveAssistant` / `cancelLocal` / `failPendingOnDisconnect`
- `healthSlice`: `{processes, hub, gateway, statusWsState, statusLastMessageAt,
  chatWsState, chatLastMessageAt, restReachable, lastRestSuccessAt}` +
  `applyWsSnapshot` / `applyRestSnapshot` / `setChannelState`
- `uiSlice`: `{consoleOpen, activeTab}` + `toggleConsole` / `setTab`
- hooks 负责副作用；**v1 串行化**：pending 时输入框可编辑但 Enter 禁用（服务端
  单连接串行 + 60s 超时，排队发送体验差）。

## Tauri 集成（v2 schema 修正）

`tauri.conf.json`（v2：`productName`/`version`/`identifier` 在**顶层**）：

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "yuki-desktop",
  "version": "0.1.0",
  "identifier": "com.yuki.desktop",
  "build": {
    "frontendDist": "../dist",
    "devUrl": "http://localhost:5173",
    "beforeDevCommand": "npm run dev",
    "beforeBuildCommand": "npm run build"
  },
  "app": {
    "windows": [{
      "title": "Yuki",
      "width": 1100,
      "height": 720,
      "minWidth": 800,
      "minHeight": 600,
      "resizable": true,
      "decorations": true
    }],
    "security": {
      "csp": "default-src 'self'; img-src 'self' asset: data:; style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8765 ws://127.0.0.1:8765 ws://localhost:5173"
    }
  },
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": ["icons/32x32.png", "icons/128x128.png", "icons/128x128@2x.png", "icons/icon.icns", "icons/icon.ico"]
  }
}
```

- **icons 必须真实存在**：`npm run tauri icon <源图>` 生成全套，空目录会导致
  `tauri build` 失败；
- **Cargo.toml 必须含 `[build-dependencies] tauri-build = "2"` + `build.rs`**
  （`tauri_build::build()`），否则编译失败；
- Rust 侧实现仅用于启动探测的 blocking HTTP 请求（v1 可直接使用
  `std::net::TcpStream`，避免向 WebView 暴露 HTTP capability），`GET /api/health` 总
  超时 1s，并校验响应同时含 `gateway/hub/processes`，不能只凭 8765 端口可连接就
  认定为 external Yuki Gateway；
- CSP 是否作用于 `devUrl` 以及是否影响 Vite HMR，以项目锁定的 Tauri v2 版本实测
  为准；若 dev 中 HMR 被拦，只在 dev 配置放宽/关闭 CSP，release 仍使用上述严格
  策略；
- v1 用 Rust `std::process::Command` 管理后端，不引入 `tauri-plugin-shell`，也不向前端
  暴露 shell capability；`capabilities/default.json` 保持 `core:default`；
- v1 Rust 诊断暂写 stderr；终端启动的 debug 版可见，Windows release GUI 通常不可见。
  v2 打包 Python runtime 时同步引入持久化日志（如 `tauri-plugin-log`），不能把
  `eprintln!` 描述成 release 可观测日志；
- **后端所有权模式**：dev/release 使用相同探测流程——启动时先探测
  `127.0.0.1:8765/api/health`；已有健康 Gateway 则进入 `external` 模式，只连接、绝不
  在退出时杀它；端口被占但不是 Yuki Gateway 时显示明确错误，不另选隐式端口；
- **自启动模式**：无现有 Gateway 时，dev 默认不自启动、release 默认自启动；
  `YUKI_DESKTOP_LAUNCH_BACKEND=true|false` 可显式覆盖对应默认值。需要自启动时，
  解释器优先取 `YUKI_PYTHON` 的绝对路径，否则尝试 `py -3`；
  启动前运行 `-c "import yuki"` 预检；失败写入 Rust 日志，Web UI 根据 REST 不可达
  显示通用的后端配置指引（v1 不引入 Tauri JS 桥），不进入无限重连；
- **工作目录**：自启动必须使用 `YUKI_WORKDIR` 的绝对路径；该目录必须存在并包含
  `config.yaml`。release 若未提供则拒绝自启动并给出配置提示，不能继承 GUI 的随机
  current directory。进程环境显式设置 `YUKI_GATEWAY_ENABLED=true`；
- **进程所有权与关闭（step 9 必做 spike，非既定可行）**：Rust 以
  `CREATE_NEW_PROCESS_GROUP` 启动并只保存自己 spawn 的 supervisor PID。Tauri GUI
  父进程通常没有控制台，而 `GenerateConsoleCtrlEvent` 要求共享控制台，直接向独立
  子进程组投递 CTRL_BREAK 可能失败；step 9 必须在 Windows release 形态实测并记录
  结果。首选 fallback 是通过 `windows-sys` 调用
  `AttachConsole(supervisor_pid) → GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT,
  supervisor_pid) → FreeConsole()`，成功后最多等待 5s；若 attach/投递失败或超时，
  立即执行 `taskkill /pid <pid> /T /F`（接受丢失优雅清理，已知失败时不必固定空等
  5s）。external 模式不做任何进程操作；
- v1 release 是“本机已安装 Yuki Python 环境”的开发者发行物，不是独立终端用户
  安装包；打包 Python runtime 与自动创建工作目录/配置属于 v2 部署范围；
- **vite.config.ts**：`server: { port: 5173, strictPort: true, host: "localhost" }`
  ——端口被占时 Vite 自动换端口会让 devUrl 失配，且 CORS regex 只放行 localhost。

## 后端文件改动（最小集合）

1. `.gitignore`：追加 `frontend/node_modules/`、`frontend/src-tauri/target/`
   （`frontend/dist/` 已被现有 `dist/` 条目覆盖，显式写也无害）；
2. `config.py`：`GatewayConfig.cors_origins` 默认同时包含 `tauri://localhost` 与
   `http://tauri.localhost`，避免 release 必须依赖用户手工改配置；同步更新
   `tests/test_config.py`；
3. `config.example.yaml`：`gateway:` 段上方加注释——
   - 桌面前端需要 `gateway.enabled: true`；
   - `cors_origins` 示例同时列出 `tauri://localhost` 与 `http://tauri.localhost`；已有
     `config.yaml` 若显式覆盖该列表，升级时也必须补 Windows origin；dev 模式
     `http://localhost:5173` 由 regex 允许；
4. `gateway.py` `_chat_message_handler` 补
   `reason/ts/spoke/emotion` 四字段 + 1 条单测。
5. `gateway.py` `_health_snapshot()` 为进程心跳补 `fresh/last_seen_age_s`，阈值使用
   `max(15s, 3 * config.health.heartbeat_interval_s)`；增加新鲜/过期边界单测。

除上述向后兼容的 DTO/CORS 修正外，不改 cognition、interrupt 或进程协议。

## 开发工作流

- **开发（推荐）**：`config.yaml` 设 `gateway.enabled: true` →
  终端 A `python -m yuki.supervisor` → 终端 B `cd frontend && npm install && npm run tauri dev`
- **仅前端（无后端）**：`cd frontend && npm run dev`（纯浏览器，fetch/WS 不受
  Tauri 影响，mock 数据可注入）
- **生产构建**：`npm run tauri build` → 安装包在
  `src-tauri/target/release/bundle/`。运行前设置 `YUKI_PYTHON`（推荐）和必需的
  `YUKI_WORKDIR`；v1 不打包 Python。

## 测试策略

**单元测试**（Vitest，与模块同级 `*.test.ts(x)`；组件用 jsdom，WS 用注入的 mock
实现 + fake timers，REST 用 msw）：
- `ConnectionManager.test.ts` — 重连退避上限 10s、仅 status 通道心跳（25s）、
  chat 通道不发 ping、消息按 type 分发、status/chat 状态互不覆盖
- `chatSlice.test.ts` — send 追加用户气泡 + 设 pending；receiveAssistant 追加
  yuki 气泡 + 清 pending；本地取消后旧 socket generation 的 chunk 丢弃；pending
  断线标记失败且不自动重发
- `healthSlice.test.ts` — WS cached hub 不覆盖权威 hub；REST 全量校准；status/chat
  时间戳隔离；fresh=false 显示过期
- `MessageBubble.test.tsx` — assistant 消息渲染 ReasonBadge（数据源按 D1）
- `ProcessCard.test.tsx` — healthy=true 绿、false 红、展开切换；hub 不可达态

**集成测试**：
- `useHealthStream.integration.test.ts` — 每 30s REST 权威刷新；75s 无 status 消息时
  立即探测；REST 可达但 WS 失效进入“状态流异常”；恢复后保持周期校准
- `useChat.integration.test.ts` — pending 断线不自动重发；本地取消关闭旧 socket、重建
  后才恢复发送，旧连接迟到回复不可进入新 generation

**v1 Windows 冒烟/e2e（不可推迟到 v2）**：
- CI 至少执行 `npm run build`、`cargo check` 与 `npm run tauri build -- --debug`；
- 本机脚本使用测试端口与临时 `YUKI_WORKDIR`：启动真实 supervisor/Tauri → 验证
  release origin 可访问 `/api/health` → 完成一次 chat → 退出应用 → 确认仅自有进程树
  退出且端口可重新绑定；
- 另测 external 模式：预先启动 supervisor，关闭 Tauri 后 supervisor 仍存活。
- Windows release 冒烟必须分别记录 CTRL_BREAK spike 的 attach、投递、5s 内退出结果，
  并至少覆盖一次强杀 fallback；若已知 attach/投递失败，断言不会无意义等待完整 5s。

## 实现顺序（每步可验证）

1. **脚手架 + 配置** — 目录树、package.json、vite.config.ts（strictPort）、Ant
   Design 暗色主题、App.tsx 空壳；更新 `.gitignore`、`config.example.yaml` 注释。
   目标：`npm run dev` 渲染暗色空窗口
2. **类型定义 + REST 客户端** — `types/api.ts`（按 §API 实际面）、`api/client.ts`
   （错误信封解析）+ `api/rest.ts`（getHealth）
3. **后端最小合约补丁** — Reason 字段、进程 heartbeat 新鲜度、Windows release
   CORS 默认值及对应 pytest
4. **WebSocket ConnectionManager** — `api/ws/*`（心跳仅 status 通道、分通道状态、
   socket generation、重连与分发）。单元测试
5. **Zustand store + slices** — request generation、分通道连接状态、REST 权威状态。
   单元测试
6. **健康面板** — `useHealthStream`（WS 订阅 + 30s REST 校准）+ HealthPanel 通用渲染。
   启动 supervisor 手动验证
7. **对话视图** — `useChat` + socket generation 本地取消 + ChatPanel 全组件。
   手动验证取消、pending 断线及不自动重发
8. **控制台壳 + 占位标签** — ConsoleDrawer、ConsoleTabs、Placeholder
9. **Tauri 集成 + Windows CTRL_BREAK spike** — src-tauri（v2 schema、
   tauri-build/build.rs、icons、后端所有权、预检、固定工作目录）；验证 dev/release
   的 external/self-owned 组合。用 release GUI 实测直接 CTRL_BREAK 是否可行；若不行，
   落地并验证 `AttachConsole + GenerateConsoleCtrlEvent + FreeConsole`，投递前临时用
   `SetConsoleCtrlHandler(NULL, TRUE)` 避免当前进程处理该控制事件，随后恢复；失败/超时
   走 `taskkill /T /F`。把实测结论与最终采用路径写入 README
10. **连接状态 + 收尾** — ConnectionStatus 四态、滚动、Enter/Shift+Enter；README
    文档化环境变量与开发/生产工作流；typecheck/lint/build/cargo check 和 v1 冒烟通过

## Out of Scope（v1）

- 不做流式对话 / thinking 事件（后端无此能力，v2）
- 不做真正的服务端打断（v2：后端接 `chat/interrupt` 订阅 + 独立 interrupt 连接）
- 不做 Memory/Soul/Config/Perception 控制台（其中 Memory/Soul 需后端新增端点）
- 不打包 Python 运行时（v2 部署工作）
- 不做 Tauri JS API 调用（v1 纯 Web 栈，capabilities 保持最小）
