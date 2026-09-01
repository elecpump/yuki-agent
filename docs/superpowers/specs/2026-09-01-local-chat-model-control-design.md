# Design: 前端运行时控制本地对话模型

Date: 2026-09-01

关联文档：

- [2026-08-27-single-main-model-worker-design.md](2026-08-27-single-main-model-worker-design.md)
- [2026-08-30-yuki-desktop-frontend-design.md](2026-08-30-yuki-desktop-frontend-design.md)

## 1. 任务分析

### 1.1 用户目标

桌面前端允许用户开启或关闭本地对话模型。关闭后不再使用本地模型做路由或回复，
并释放 `local_chat` 占用的模型资源；重新开启成功后恢复本地路由与回复。

### 1.2 控制对象

本功能只控制 model catalog 中的 `local_chat`，UI 使用“本地对话模型”这一名称。
它不停止 `model_worker` 进程，也不影响 VLM、STT、TTS 或 embedding。

不能通过停止 `model_worker` 实现本功能：该进程同时承载其他模型，而且 supervisor 会把
退出的 worker 自动拉起。

### 1.3 现状与缺口

- `model_worker` 已有异步模型操作：`models/operations/submit|status|cancel`，支持
  `load/unload/reload/preflight`。
- `ModelManager.run_inference()` 在每次推理前自动调用 `load()`。因此单纯 `unload`
  只表示释放当前驻留资源，下一次本地请求会重新加载，不能表达“关闭”。
- cognition 是否走本地路径由 `DecisionHub._local_enabled` 决定，该值目前只在启动时从
  `local_brain.enabled` 初始化。
- `local_brain.enabled=false` 时 assembler 不构造 local router/composer，底层模型也以
  disabled 状态注册。因此运行时开关只能控制启动时已配置为可用的能力，不能替代静态配置。
- gateway 尚未暴露模型控制接口；前端只有健康面板和通用 REST client。

### 1.4 术语边界

“本地对话模型”是本功能对 `local_chat` 技术能力的 UI 名称；enabled、recovering、disabling
等是运行时控制状态，不是 Thread、记忆或关系领域概念，因此不扩展 `CONTEXT.md` 的领域词汇。

## 2. 不在范围内

- 不编辑或持久化 `config.yaml`。
- 不控制整个 `model_worker` 的进程生命周期。
- 不开放任意模型、任意 action 的通用管理 API。
- 不让用户直接控制 VLM、STT、TTS、embedding。
- 不在 `local_brain.enabled=false` 的运行实例中动态创建 router/composer。

## 3. 核心决策

1. `local_brain.enabled` 继续表示启动时是否装配本地对话能力；前端开关表示当前 yuki
   主进程生命周期内的运行时目标状态。yuki 主进程重启后恢复配置值；model_worker 单独
   重启时，主进程必须重新落实仍然有效的运行时目标。
2. worker 新增动态 `enable/disable` 状态。disabled controller 必须拒绝自动 `load()`，
   从根源上保证关闭后不会因下一次推理重新加载。
3. cognition 是跨层编排者：它同时持有 `DecisionHub` 和 `RemoteModelRegistry`，负责按安全
   顺序切换路由与 worker 模型状态，并在 worker 单独重启后执行 reconciliation。
4. gateway 只暴露固定的本地对话模型控制契约，不把内部通用模型管理面直接暴露给前端。
5. 操作保持异步。HTTP 提交返回 `202`，前端轮询状态；操作完成不依赖前端持续在线，
   cognition 内部 watcher 必须完成最终路由切换或失败收敛。

## 4. 安全状态机

### 4.1 状态

对外状态统一为：

- `unavailable`：启动配置禁用，运行时不可开启。
- `disabled`：本地路由关闭，worker controller 禁止自动加载，模型不驻留。
- `enabling`：worker 正在 enable + load，本地路由仍关闭。
- `enabled`：模型 health 明确 `callable=true` 且本地路由开启。
- `disabling`：目标为关闭且本地路由已关闭，但 worker 尚未确认 disabled；包含等待提交、
  worker 重启后短暂 warmup、drain、unload 和 disable 的完整收敛窗口。
- `recovering`：目标仍为开启，但 worker/transport 中断或有截止时间的 circuit-open 导致当前
  不可用；路由保持关闭，reconciler 正在恢复模型。
- `failed`：最近一次切换失败；返回稳定的 `enabled` 实际值与错误码，允许使用新的
  idempotency key 重试。

### 4.2 关闭顺序

```text
PUT enabled=false
  → 原子关闭 DecisionHub 的新本地路由
  → 提交 worker disable(local_chat)
  → worker 拒绝新 lease，等待 active_calls 排空
  → unload 模型
  → controller 进入 disabled
```

若 drain/unload 失败，路由保持关闭，状态进入 `failed`。这是安全降级：不得为了恢复旧状态
而重新放行本地请求。

### 4.3 开启顺序

```text
PUT enabled=true
  → 保持 DecisionHub 本地路由关闭
  → 提交 worker enable(local_chat)
  → controller 进入 unloaded 后加载模型
  → load 成功且模型可接受调用
  → 原子开启 DecisionHub 本地路由
```

若 enable/load operation 以确定性错误失败，本地路由保持关闭，状态进入 terminal `failed`，
等待用户使用新的 idempotency key 重试。

### 4.4 worker 重启与状态收敛

`LocalChatControl` 保存的目标状态位于 yuki 主进程，不随 model_worker 单独重启丢失。
后台 reconciler 定期读取 worker/model health，并把 service 暂时不可达视为 worker 边界重启：

- 目标为关闭，但重启后的 controller 不是 disabled：路由继续保持关闭，并重新提交 disable。
- 目标为开启，但 worker/transport 暂不可用或模型处于有截止时间的 circuit-open window：先关闭
  路由，状态进入 recovering；worker 可达或 circuit 到期后重新 enable + load，成功后再开放路由。
- `enabled` 只能在 `target_enabled=true`、路由已开放且 worker health 的 `callable=true` 时返回。
- `disabled` 只能在 `target_enabled=false`、路由关闭且 worker runtime 为 disabled 时返回。
- 其余组合必须表现为 enabling/disabling/recovering/failed，不能输出自相矛盾的稳定状态。

reconciler 使用有界指数退避，不忙轮询，也不因前端关闭而停止。model_worker 重启后可能短暂
重新 warmup `local_chat`，但本地路由始终保持关闭，reconciler 随后再次 disable 并释放资源。

自动 recovering 只处理 transport/service unavailable、worker operation 因重启消失，以及带明确
`retry_after` 的 circuit-open。worker operation 返回 `load_failed`、`unload_failed`、
`drain_timeout`、`insufficient_vram` 等确定性失败时进入 terminal failed，不再自动提交；用户发起
新的切换请求后才重试。由此保证 failed 不会暗中重试，recovering 也有明确恢复条件。

worker operation 返回 `cancelled`（例如 worker 优雅关闭取消 queued operation）、status 返回
`operation_not_found`，或 status RPC 发生 timeout/unavailable 时，control operation 均进入
recovering，并由 reconciler 在 worker 可达后重新提交。只要 operation status 仍可达且为 running，
就不凭固定时长误判长时间模型加载；worker crash 后旧 store 消失，最终会由 RPC 中断或
operation_not_found 暴露。

### 4.5 并发与幂等

- 同一时刻最多有一个本地模型切换操作。
- 请求必须携带 `idempotency_key`；control 层保存 key 与 target 的绑定。相同 key + 相同 target
  返回相同 operation，相同 key + 相反 target 返回 `409 idempotency_key_conflict`。
- 操作进行中收到相同目标请求时返回当前 operation；收到相反目标请求时返回
  `409 local_model_operation_in_progress`，不做隐式取消或反转。
- `DecisionHub` 的开关读写使用锁或原子快照；已经进入 local inference 的调用由 worker
  drain 机制处理，新请求在关闭第一步后直接走 cloud/notice。

## 5. 后端设计

### 5.1 model_worker

`ModelController` 增加线程安全的运行时 enable/disable 转换：

- `enable()`：允许从 disabled 进入 unloaded；FAILED 且没有可用 handle 时清除失败状态后进入
  unloaded。manager 随后显式 load，不直接接受推理。
- `disable()`：先执行现有 drain/unload，再进入 disabled；FAILED 状态允许重试清理，保证失败
  后仍有收敛路径。
- `load()` 在 disabled 状态继续明确抛出 `ModelUnavailableError`。

`ModelManager` 增加 `enable(model, load=True)` 与 `disable(model)`。内部 operation handler 支持
enable/disable，但 `models/operations/submit` 的通用 `ALLOWED_ACTIONS` 保持不变，不允许调用者
对任意模型执行新动作。worker 另注册固定目标的 wire 服务：

```text
models/local-chat/control {enabled, idempotency_key, reason?}
```

该服务只能操作 `local_chat`，内部把 boolean 映射到 enable/disable operation；operation 状态仍
复用 `models/operations/status`。这是 yuki ↔ model_worker 的受认证进程边界协议，不暴露为通用
Gateway REST 管理面。

`enable` operation 执行 controller enable + load，确保 cognition 只在 load 成功后开放路由。
`disable` operation 执行 drain + unload + controller disable。

`ModelOperationStore` 当前会把所有 handler 异常压成 `operation_failed`，无法支持第 4.4 节的
恢复分类。新增 `ModelOperationFailure(error_code)`：operation handler 将 controller/manager 的
类型化失败映射为稳定错误码（至少 `load_failed`、`unload_failed`、`drain_timeout`、
`insufficient_vram`、`model_disabled`），store 单独捕获并保存其 `error_code`；未知异常仍降级为
`operation_failed`。分类依据必须来自异常类型或 controller 状态，不解析异常文本。

健康详情继续复用 `runtime_state`，并增加明确的 `runtime_enabled`，避免前端从
`loaded=false` 猜测 disabled/unloaded；同时增加由 worker 计算的 `callable`：仅当 runtime state
为 ready/degraded、`accepting_calls=true`，且 circuit 未打开时为 true。cognition 不自行解释
worker 的 monotonic `retry_after` 数值。

### 5.2 cognition

新增 `LocalChatControl`（独立小模块），职责为：

- 保存配置可用性、目标状态、实际路由状态、control operation 与最近错误。
- 调用新增的 `RemoteModelRegistry.set_local_chat_enabled()` 专用方法；operation 查询继续复用
  `operation_status()`。不得用会被通用 action 白名单拒绝的 `submit_operation()` 提交开关。
- 在内部后台 watcher/reconciler 中完成操作收敛和 worker 重启恢复，不依赖 gateway 或前端轮询。
- 按第 4 节顺序调用 `DecisionHub.set_local_enabled()`。
- 将 `BusTimeoutError` 归一为 `model_worker_timeout`，其他 worker `BusError` 归一为
  `model_worker_unavailable`，不向 gateway 泄漏原始异常。

control operation 使用 cognition 自己的 operation id，并保存
`{idempotency_key, target_enabled, worker_operation_id, state, error_code}`。worker operation id 只是
内部执行细节：worker 重启导致它消失时，control operation 进入 recovering，reconciler 根据目标
和健康状态重新提交，最终更新原 control operation。已完成 control operation 按有界 TTL 清理。

`DecisionHub` 增加线程安全的：

```python
def set_local_enabled(self, enabled: bool) -> None: ...
def local_enabled(self) -> bool: ...
```

cognition 不把 control 注册到 `LocalServiceRegistry`，因为 bridge 会把 registry 中的服务镜像到
wire bus。`CognitionAssembler` 将 `LocalChatControl` 放入 `CognitionRuntime`，
`CognitionAgent` 暴露只读属性，`YukiApp` 在启动 gateway 时直接注入同进程对象。测试可向
`GatewayRuntime` 注入 fake control。

当 `local_brain.enabled=false` 时，status 返回 `available=false`，set 返回领域错误
`local_model_config_disabled`。

### 5.3 gateway REST API

#### `GET /api/local-model`

```json
{
  "available": true,
  "enabled": true,
  "target_enabled": true,
  "state": "enabled",
  "runtime_state": "ready",
  "loaded": true,
  "active_calls": 0,
  "operation": null,
  "last_error": ""
}
```

#### `PUT /api/local-model`

Request:

```json
{"enabled": false, "idempotency_key": "frontend-generated-uuid"}
```

Accepted response（HTTP 202）：

```json
{
  "operation_id": "...",
  "accepted": true,
  "target_enabled": false
}
```

#### `GET /api/local-model/operations/{operation_id}`

```json
{
  "operation_id": "...",
  "target_enabled": false,
  "state": "running",
  "error_code": null
}
```

`state` 枚举为 `queued`、`running`、`recovering`、`succeeded`、`failed`。

错误映射：

- 配置禁用：`409 local_model_config_disabled`
- 相反操作正在执行：`409 local_model_operation_in_progress`
- idempotency key 被另一目标使用：`409 idempotency_key_conflict`
- operation 不存在或已过期：`404 local_model_operation_not_found`
- model_worker 不可达：`503 model_worker_unavailable`
- worker RPC 超时：`504 model_worker_timeout`
- 非法请求：FastAPI `422`

gateway 使用现有统一错误信封，不返回内部异常文本。503/504 来自 control 层对
`BusError`/`BusTimeoutError` 的显式分类，不依赖字符串猜测。接口固定操作 `local_chat`，请求体
不接受 model name 或 action。

## 6. 前端设计

### 6.1 入口

在控制台“健康”页增加独立的 `LocalModelControlCard`，放在 Gateway/Bus Hub 卡片之后、
通用进程卡片之前。它不修改通用 `ProcessCard`，避免把产品操作硬编码进健康组件。

卡片标题为“本地对话模型”，包含：

- Switch：开启/关闭。
- 状态 Tag：配置禁用、已关闭、开启中、已开启、关闭中、恢复中、失败。
- 辅助信息：模型是否驻留、active calls、最近错误。
- 操作期间 Switch disabled，并展示 Spin/“正在排队或切换”。
- 辅助说明：“运行时设置；Yuki 重启后恢复配置值”。worker 单独重启不改变目标状态。

### 6.2 状态与请求

新增独立 `localModelSlice`，不把控制操作塞入 `healthSlice`：

- `status`
- `loading`
- `operationId`
- `error`
- `refresh()`
- `setEnabled(enabled)`

新增 `useLocalModelControl` hook：

1. 组件 mount 时 GET status。
2. 点击 Switch 时用 `crypto.randomUUID()` 生成 idempotency key 并 PUT。
3. 使用 `config/runtime.ts` 中的 `LOCAL_MODEL_OPERATION_POLL_MS=500` 查询 operation，终态后
   立即刷新 status；组件卸载时停止轮询。
4. 网络失败或 operation 过期时刷新权威 status，不做乐观状态提交。
5. 防止重复点击；失败时保留服务端实际状态并显示可重试错误。

现有 `/ws/status` 仍用于进程健康展示，不作为控制操作完成信号。

## 7. 预计修改范围

后端：

- `src/yuki/model_worker/controller.py`
- `src/yuki/model_worker/manager.py`
- `src/yuki/model_worker/operations.py`
- `src/yuki/model_worker/services.py`
- `src/yuki/model_client.py`
- `src/yuki/cognition/brain/hub.py`
- `src/yuki/cognition/local_model_control.py`（新增）
- `src/yuki/cognition/assembly.py`
- `src/yuki/cognition/agent.py`
- `src/yuki/bus_server/gateway.py`
- `src/yuki/app/main.py`

前端：

- `frontend/src/types/api.ts`
- `frontend/src/api/rest.ts`
- `frontend/src/config/runtime.ts`
- `frontend/src/state/slices/localModelSlice.ts`（新增）
- `frontend/src/state/store.ts`
- `frontend/src/state/hooks/useLocalModelControl.ts`（新增）
- `frontend/src/components/console/health/LocalModelControlCard.tsx`（新增）
- `frontend/src/components/console/health/HealthPanel.tsx`
- `frontend/src/styles/global.css`（仅在现有样式不足时）

测试：

- `tests/model_worker/test_controller.py`
- `tests/model_worker/test_manager.py`
- `tests/model_worker/test_operations.py`
- `tests/model_worker/test_services.py`
- `tests/cognition/test_hub.py`
- `tests/cognition/test_local_model_control.py`（新增）
- `tests/cognition/test_assembly.py`
- `tests/cognition/test_cognition.py`
- `tests/bus_server/test_gateway.py`
- `tests/app/test_main.py`
- `frontend/tests/client.test.ts`
- `frontend/tests/localModelSlice.test.ts`（新增）
- `frontend/tests/useLocalModelControl.integration.test.tsx`（新增）
- `frontend/tests/LocalModelControlCard.test.tsx`（新增）
- `frontend/tests/HealthPanel.test.tsx`

## 8. 实现顺序（每步可验证）

1. **worker 状态语义**：先为 controller/manager 写失败测试，再实现 enable/disable、FAILED
   重试和“disabled 后禁止自动加载”。只运行 model_worker controller/manager 测试。
2. **受限进程边界协议**：为 operation store 增加结构化失败码，增加固定 `local_chat`
   control service 与 client 封装；验证通用 submit 仍拒绝 enable/disable，专用服务的幂等、
   状态查询和错误分类通过。
3. **cognition 编排**：为 `DecisionHub` 增加动态开关，实现 `LocalChatControl` 自有 operation、
   watcher 和 reconciler，再通过 assembler/agent 暴露进程内对象。用 fake registry 验证切换
   顺序、失败收敛和 worker 可用性中断。
4. **gateway 注入与 REST**：`YukiApp` 将 control 注入 gateway；实现三个固定 REST 接口和
   领域错误映射，跑 gateway/app 测试。
5. **前端数据层**：补 DTO、REST client、slice 与 hook，覆盖轮询、卸载清理、幂等 key 和
   错误恢复。
6. **前端 UI**：加入 `LocalModelControlCard`，覆盖所有状态、禁用逻辑和运行时说明。
7. **集成验证**：依次运行相关 Python 测试、默认测试、相关 e2e，以及前端 test/typecheck/
   lint/build；最后手动验证关闭释放显存、消息不触发重载、worker 重启后重新收敛。

每一步保持可独立回归；后一步不以修改前一步测试断言来掩盖状态机错误。

## 9. 测试策略

按 TDD 实现，测试先于对应代码：

1. controller/manager：disable 后推理不得自动重载；enable 成功、重复操作幂等；活跃 lease
   drain；失败状态。
2. worker services：通用 submit 拒绝 enable/disable；专用 local_chat control 只接受 boolean
   目标，operation 状态与结构化错误码稳定；未知异常仍为 `operation_failed`。
3. DecisionHub：关闭后不调用 local router；开启后恢复；并发读写安全。
4. cognition control：关闭顺序、开启顺序、worker 失败回滚、配置禁用、相反操作冲突、
   idempotency key 目标冲突、watcher 不依赖客户端轮询。
5. worker 单独重启：目标关闭时重新 disable，目标开启时 recovering→enabled，且恢复完成前
   路由不开放。
6. gateway：三个 REST 合约、202/404/409/503/504 映射、固定 local_chat 边界。
7. 前端 REST/slice/hook：queued→running→succeeded、failed、重复点击、unmount
   停止轮询。
8. UI：各状态文案、Switch checked/disabled、运行时说明、错误展示及成功刷新。
9. 完整 Python 默认测试、相关 e2e、前端 test/typecheck/lint/build。

## 10. 风险与已知限制

- **yuki 主进程重启会重置开关**：运行时目标不持久化，重启后恢复
  `local_brain.enabled`。UI 必须常驻显示该说明；持久化配置属于另一个需求。
- **model_worker 重启有短暂资源窗口**：worker 会按静态 policy 启动/warmup；当目标为关闭时，
  cognition 路由仍保持关闭，reconciler 会再次 disable，但显存可能短暂被占用。
- **drain timeout**：关闭时若活跃推理无法排空，worker operation 可能失败；路由继续关闭，
  UI 显示失败并允许重试，不能自动恢复本地路由。
- **最终一致性**：REST control 状态比 worker heartbeat 更权威；健康 WS 可能延迟数秒，UI 不得
  用 heartbeat 覆盖 control 状态。
- **operation TTL 与 worker 重启**：worker operation 记录会 cancelled、过期或随重启消失；
  cognition 保留 control operation，并由 reconciler 根据目标状态与模型健康重新提交，不把
  `cancelled` 或 `operation_not_found` 当成目标已完成。
- **本地管理面安全**：gateway 仍只应绑定 localhost/CORS 白名单；REST 固定 local_chat 与
  boolean，不暴露 worker 的通用模型 action。
- **启动配置禁用**：`local_brain.enabled=false` 时没有 router/composer，运行时不能开启；UI
  显示配置禁用和重启配置提示。

## 11. 流程附加：代码审查阶段

实现和验证完成后，交给独立 review agent 检查：

- 状态机竞态与锁顺序。
- 关闭期间是否仍可能产生新 local inference。
- 异步 operation 是否可能因前端断开而不收敛。
- API 是否越权暴露其他模型管理能力。
- 前端轮询清理、重复请求和错误恢复。
- 测试是否覆盖用户可见行为，而非只覆盖内部方法。

审查发现的问题先修正并回归测试，再向用户交付。
