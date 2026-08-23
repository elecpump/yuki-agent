# Design: Yuki 桌面端 Gateway 修订版（v1 / v2 分阶段）

Date: 2026-08-23

## Goal

在现有双进程架构上为桌面前端（Tauri + React）提供 HTTP REST + WebSocket 网关，
并明确哪些能力是"纯透传"、哪些是"新增工程量"。

## 总体判断（相对原方案的修订）

原方案骨架成立，但有六处必须修正：

1. **"零 IPC 开销"不成立**。BusHub（`src/yuki/bus.py:106`）是纯 ZMQ 转发进程
   （XSUB/XPUB/ROUTER），无应用级 API。gateway 即使嵌入 bus_server，也需自建
   `BusNode` 连回 hub——同进程 TCP loopback，省的是"少一个进程"，不是 IPC。
2. **流式对话是现有系统没有的能力**。回复为单条 publish（`cognition/brain/hub.py:185`
   → REPLY `{text, ts}`），无 thinking/tool_call/token 流。必须拆阶段：v1 单条，
   v2 流式（cognition 新增流式产出能力）。
3. **config 无法热生效**。各进程启动时 `Config.from_env()` 加载一次。v1 只做
   "持久化 + 重启生效"，热重载列为 v2。
4. **history / agent_state / thinking / 帧流 等数据源不存在**，均为新增能力，
   非透传。逐项见分阶段表。
5. **`event/reply` 不能做无 correlation 的 chat 匹配**。现有 REPLY 只有
   `{text, ts}`，没有 `session_id/task_id`，且可能来自语音、热键、情境主动回复。
   v1 chat 必须走 `cognition.chat` RPC，或显式补 correlation id。
6. **bus_server 健康不能通过 `register_health=True` 直接获得**。`BusServerAgent`
   的 `bus` 是 `BusHub`，不是 `BusNode`，不能挂普通 `HealthReporter.respond`。
   gateway 健康应由 gateway 自己的 `BusNode` 暴露，或只聚合 `BUS_HEALTH_SERVICE`。

## 架构（修订）

```
Tauri (Rust)                 ── 只管理 supervisor 一个 sidecar
  └─ WebView (React)
       ├─ HTTP REST → localhost:8765
       └─ WS        → localhost:8765

bus_server 进程（bus_server agent 内嵌 gateway，不新增进程）：
  ├─ BusHub（现有：XSUB/XPUB/ROUTER 转发 + REQ/REP 路由）
  └─ Gateway（新增）
       ├─ FastAPI/uvicorn（asyncio，专用后台线程，可 stop/join）
       ├─ ConnectionManager（WS 会话注册 + 僵尸清理）
       └─ 自有 BusNode 客户端（连回本进程 BusHub，经 ZMQ 与
           cognition/perception/interaction 等跨进程通信）
```

关键点：

- **gateway 与 hub 通信必须经 BusNode**（同进程 socket 复用，无需新进程），
  不要试图"直接调 BusHub 内部"。
- **asyncio ↔ ZMQ 桥**是核心集成点：BusNode 订阅回调在 ZMQ 线程，WS 推送在
  asyncio；用 `loop.call_soon_threadsafe` / 每通道一个 `asyncio.Queue` 转交。
- gateway 启动在 `BusServerAgent.setup()`：封装 `GatewayServer.start()/stop()`，
  内部保存 `uvicorn.Server`，stop 时设置 `server.should_exit=True` 并 join 线程；
  teardown 顺序为 `gateway.stop()` → `gateway_bus.close()`。
- 不把 `BusServerAgent.register_health` 直接改为 True：`BusHub` 不是普通
  `BusNode` responder。健康面保留 `BUS_HEALTH_SERVICE` 检查 hub；如需 gateway
  自身健康，则由 gateway 的自有 `BusNode` 注册 `health/gateway`。

## 依赖与配置

- `pyproject.toml` 新增 `desktop` extra：`fastapi`、`uvicorn[standard]`、`websockets`
- 新增 `GatewayConfig`（挂 `Config.gateway`）：
  `enabled=false`、`host="127.0.0.1"`、`port=8765`、
  `cors_origins=["tauri://localhost"]`、`cors_origin_regex="^http://localhost:\\d+$"`、
  `ws_heartbeat_timeout_s=45`、`cleanup_interval_s=30`、
  `chat_task_timeout_s=60`、`history_dir="data/recordings"`
- `gateway.enabled` 默认关闭，避免未安装 `desktop` extra 的核心运行路径
  (`python -m yuki.supervisor` / `python -m yuki.bus_server`) 因 FastAPI/uvicorn
  缺失而崩溃；Tauri/桌面打包配置显式打开。
- CORS 白名单放 Tauri WebView；localhost 端口通配用 `allow_origin_regex`，
  不依赖 `cors_origins` 里的 `"http://localhost:*"` 字符串匹配。
- 认证 v1 仅限本地绑定 `127.0.0.1`；后续加 token。若允许非 localhost 绑定，
  token 必须提前进入 v1。

## REST API

### v1（透传或小改造即可落地）

| 端点 | 说明 | 实现路径 |
|------|------|----------|
| GET `/api/health` | 进程健康 | gateway 订阅 HEARTBEAT（`health.py:69` 已有 5s 心跳）聚合缓存 + `BUS_HEALTH_SERVICE` RPC |
| GET `/api/memory` | 记忆列表 | 代理现有 `memory/list` RPC（`register_memory_services`） |
| GET `/api/memory/{id}` | 记忆详情 | 代理现有 `memory/get` RPC |
| DELETE `/api/memory/{id}` | 删除记忆 | 代理现有 `memory/delete` RPC |
| GET `/api/history/sessions` | 会话列表 | 扫描 `GatewayConfig.history_dir` 下 recorder `Session` 输出目录；若 recorder 未启用或目录不存在，返回空列表 + `degraded=true` |
| GET `/api/history/{session_id}` | 某会话 turns | 解析该 session 的 `events.jsonl`（过滤 `event/perception/user_utterance` 与 `event/reply`）；注意 recorder 当前不在 supervisor 默认子进程中，v1 需新增 `RecorderConfig` 并纳入 supervisor，或 gateway 自己记录 chat history |
| POST `/api/chat` | 非流式对话 | **新增 `cognition.chat` RPC 服务**（cognition 侧注册 `bus.respond`，接收 `{text, session_id}`，复用 `_handle(UTTERANCE)`，返回 `{text, ts}`）；gateway 生成 task_id → `bus.request` → 存结果；GET `/api/chat/{task_id}` 返回。比 USER_UTTERANCE+REPLY 匹配更干净（request/response 天然相关，`interaction` 的 hotkey 已用同模式） |
| GET `/api/chat/{task_id}` | 查询异步 chat 结果 | gateway 内存 task store，完成后返回 `{status,result,error}`；超时由 `chat_task_timeout_s` 控制 |
| GET `/api/config` | 读取公开配置 | bus_server 本进程 `Config` 转 `PublicConfig` DTO；必须脱敏 `bus.auth_token`、本地敏感路径和未来 secret，不直接全量 `model_dump()` |
| GET `/api/soul` | 读取 soul | 代理 cognition 新增 `soul.get` RPC（cognition 进程持有 SoulStore） |
| GET `/api/perception/status` | 感知能力 | 订阅 HEARTBEAT 里的 perception components |

### `cognition.chat` RPC 合约

v1 新增 `cognition.chat`，作为 REST/WS chat 的唯一对话入口：

```json
// request
{"text": "你好", "session_id": "ui-session", "task_id": "gateway-task"}

// response
{"text": "你好呀。", "ts": 123.45, "spoke": true, "reason": "utterance"}
```

实现要求：

- 在 cognition 侧注册 `bus.respond("cognition.chat", handler)`。
- handler 复用 `DecisionHub._handle(TriggerKind.UTTERANCE, ..., publish_reply=False)`，
  返回结果给 gateway，但不发布 `Topics.REPLY`，避免触发 interaction/TTS 或混入
  其它 REPLY。
- gateway 负责 task 状态、超时、WS 单条 `assistant_chunk(done=true)` 包装。

### v2（需 cognition/perception 新增能力）

| 端点 | 需要的新能力 |
|------|-------------|
| PUT `/api/config` | 持久化 yaml + 经 supervisor 重启相关子进程（或热重载机制） |
| PUT `/api/soul` | cognition `soul.update` RPC（写 SoulStore + 触发 persona_refresh） |

## WebSocket 通道

### v1

- **`/ws/status`**：订阅 HEARTBEAT 推送 `health` 事件；推送使用 gateway 缓存的
  进程心跳快照，不在订阅线程里同步请求 hub health。ConnectionManager 由服务端
  主动发送 `ping` 并按 `ws_heartbeat_timeout_s`/`cleanup_interval_s` 清理断线或
  僵尸连接；status/perception 通道同时监听客户端关闭帧，断连后立即注销队列。
  `agent_state` v1 只发 `idle/error`（bus 失联），`listening/thinking/speaking`
  属 v2 状态聚合。
- **`/ws/chat`**：收 `user_input` → 创建 `task_id` → 调 `cognition.chat` RPC
  → 返回单条 `assistant_chunk(text, done=true, task_id=...)`。不要走
  `Topics.USER_UTTERANCE` + `Topics.REPLY` FIFO 匹配：现有 REPLY 没有 correlation id，
  且可能混入语音、热键或情境主动回复。`interrupt` 先标记 gateway task 为
  `cancel_requested` 并发布 `chat/interrupt`（v1 仅记录，真正中止属 v2）。
- **`/ws/perception`**：v1 只推 `foreground`（订阅 FOCUS_CHANGED）与
  `text_extract`（订阅 SITUATION_UPDATE 的 fast layer）；**二进制帧流 v2**
  （现有语义是 serve-only-latest，`capture.py`/FrameStore 无持续帧流）。

### v2（全部依赖 cognition/perception 新增能力）

| 通道/类型 | 需要的新能力 |
|-----------|-------------|
| `/ws/chat` 流式 chunk | cognition 流式产出（local composer / cloud bridge 逐片发布 + REPLY 拆片） |
| `/ws/chat` thinking / tool_call | hub `_handle` 各阶段事件发布 |
| `/ws/status` agent_state | cognition 状态聚合发布（idle/listening/thinking/speaking） |
| `/ws/perception` 二进制帧 | 事件驱动帧流新 topic（当前只 serve latest） |

## 进程生命周期（可行，需明确边界）

1. **Tauri v2 sidecar 配置**：原设计 allowlist 是 v1 写法；v2 用 capabilities/
   permissions（`shell:allow-execute` + scope）。
2. **信号**：Windows 下 Tauri 终止 sidecar 实际信号需实测（SIGBREAK/CTRL_BREAK
   是否触发 `ShutdownManager` 级联关闭）；若 sidecar 直接 kill，supervisor 的
   `terminate_children` 兜底。
3. **部署**：Tauri 生产打包需捆绑 Python 运行时或要求本机预装 yuki；sidecar
   python 路径解析在配置中显式化。
4. L1/L2 两级健康监控照原设计：Tauri 每 5s GET /api/health，超时重启整个
   sidecar；WS 由 gateway 主动 ping，断线前端"重连中"。子进程重启全部由
   supervisor 负责。
5. Gateway 线程必须是可控后台线程，不用裸 daemon。`GatewayServer.stop()`
   需要关闭 uvicorn、取消 WS 清理任务、关闭 gateway_bus，并释放端口，避免测试和
   Tauri 重启时端口占用。
6. bus_server 健康分两层：hub 健康走 `BUS_HEALTH_SERVICE`；gateway 健康走
   `health/gateway` 或 `/api/health` 内部状态。不要让 `BusHub` 冒充 `BusNode`
   注册普通 health service。

## 分阶段范围

| 阶段 | 内容 | 新增工程量 |
|------|------|-----------|
| **v1** | gateway 基础设施（FastAPI+WS 嵌入、BusNode 桥、ConnectionManager、错误格式、CORS、GatewayConfig/PublicConfig）、health/memory/config-GET/history/感知状态 REST、`cognition.chat` RPC、WS status(health)+chat(单条 RPC)+perception(foreground/text)、Tauri 生命周期、可选 recorder supervisor 接入或 gateway chat history | 中：桥接、RPC 与生命周期是主成本 |
| **v2** | 流式 chat、thinking/tool_call、agent_state 聚合、感知二进制帧流、config/soul PUT（重启生效）、interrupt 真正中止 | 大：cognition 流式能力是核心 |

## 验证

- v1：新增 `tests/bus_server/gateway/`（FastAPI 用 `TestClient`，BusNode 用
  FakeBus 注入）；`pytest` 全量绿；e2e 冒烟：起 supervisor，curl REST 全端点 +
  WS 收 HEARTBEAT 与单条回复。
- v2：流式/thinking/agent_state 各有 cognition 侧单测 + WS 集成。

## Out of Scope

- 不做 token 级流式 v1
- 不做 config 热重载 v1（只持久化）
- 不做跨进程共享内存/二进制帧 v1
- 不做多租户/外部认证
