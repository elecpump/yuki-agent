# 单主进程 + 模型独立进程 架构设计

> 日期：2026-08-27
> 状态：代码实施完成，目标硬件验收中（双进程主链路、进程内 runtime、model worker、资源管理、
> supervisor 拓扑及双向强杀恢复 e2e 已落地；真实 GPU 显存压测仍需在部署硬件上完成）
> 范围：进程拓扑（4 进程 → 2 进程）、本地通信、模型边界、兼容桥、模型调度与资源管理、supervisor 变更

## 实施与验收状态（2026-08-28）

- 迁移步骤 1—14 的代码、配置、兼容入口与文档均已完成；进程内实时链路不依赖 wire Bus，Bus 仅保留在
  进程边界、健康探活和外部兼容访问。
- operation 关闭会拒绝新提交并取消 queued 项；TTS job 会在无访问超时后由后台回收，并在 worker 关闭时
  停止准入和唤醒等待方。
- 自动化验证：默认测试集 `750 passed, 4 deselected`；e2e `3 passed, 1 skipped, 750 deselected`。
  e2e 已覆盖双进程探活、`--trigger-after`、强杀 model worker 后仅重启 worker，以及强杀 yuki 后保持
  worker PID 不变并完成 BusHub/服务重注册。
- OOM 重试、驱逐、context-fatal → `worker_fatal` 与 supervisor 的 unhealthy 定向重启由故障注入测试覆盖。
- 尚未完成的是部署环境验收，而非代码缺口：真实 GPU 峰值显存、碎片化、推理延迟与模型抖动需要在目标
  显卡和模型文件齐备后压测校准；真实 STT e2e 还依赖可选的 `soundfile` 包。
- **后续决定（评审清理）**：`models.backend=local` 模式、三层旧独立入口（`python -m
  yuki.cognition/perception/interaction`）、`cognition/model_registry.py` + `model_service.py`、
  `WireRuntimeBusAdapter` 与 supervisor 旧 tick 回退路径已全部移除，代码收敛为纯 remote 双进程形态；
  模型对象的调用统计钩子改为挂载 `ModelManager`（`cognition/call_tracker.py::CallTracker`）。
  本节的 "local 模式"、"旧入口保留" 等表述仅作为历史决策记录。

## 背景与目标

现状为 4 进程拓扑：`bus_server` + `perception` + `cognition` + `interaction`，由 supervisor 看门狗统一管理。
每层内部已是多线程（BusNode 4 个循环线程 + 每订阅 1 个 handler 线程、模型 warmup 线程、pipeline/看门狗线程、
TTS 控制器线程等），"单线程"不可行；本设计的目标是**单主进程 + 模型独立进程**：

1. perception / cognition（非推理部分）/ interaction 合并进一个主进程，实时链路改用内存事件与本地服务，
   不再依赖 ZeroMQ Bus 完成进程内通信。
2. 所有本地模型推理（VLM / STT / 本地脑 / IndexTTS / embedding）收敛到独立的 `model_worker` 进程，
   保留对最脆弱组件（torch/CUDA 推理、OOM）的故障隔离与独立重启能力。
3. Bus 只保留在进程边界：主进程 ↔ model_worker、supervisor 健康探活以及外部兼容访问。
   现有总线协议、服务名与 wire payload 形状不变（沿用 `proto/yuki.proto`）。

## 目标架构

```
supervisor（看门狗，2 个子进程）
├── yuki 主进程            python -m yuki.app
│   ├── BusHub 线程        ← 原 bus_server 内嵌（proxy/router 线程不变）
│   ├── LocalRuntimeBus    ← 进程内事件队列 + 本地服务注册表，不做 protobuf/ZeroMQ
│   ├── RemoteBusNode      ← 仅跨进程模型调用、健康探活与兼容桥接
│   ├── BusCompatibilityBridge ← 将必要的本地服务/事件暴露为既有 wire 契约
│   ├── PerceptionAgent 线程  ← 含唤醒词；音频/帧使用本地原生 payload
│   ├── CognitionAgent 线程  ← 决策/记忆/上下文/云桥；内部事件走 LocalRuntimeBus
│   ├── InteractionAgent 线程 ← 热键/唤醒词触发/TTS 播放控制；TTS 模型走客户端
│   └── Gateway 线程       ← gateway.enabled 时内嵌，直接调用本地 runtime
└── model_worker 进程      python -m yuki.model_worker
    ├── ModelManager + ModelController[]              ← 模型状态机与生命周期权威来源
    ├── ModelInferenceScheduler + GpuMemoryMonitor    ← 优先级队列、推理租约与显存准入/驱逐
    ├── ModelOperationStore                           ← 异步管理操作、幂等与结果保留
    ├── VisualUnderstander (VLM) / SpeechRecognizer (STT) / LocalChatModel (本地脑)
    ├── IndexTTSModel（流式合成）
    ├── embedding indexer（仅 memory.vector_enabled 时）
    └── 总线服务：models/*（兼容服务 + 异步管理）+ model/*（推理服务）
```

进程拓扑对比：

| 现状 | 目标 |
|---|---|
| 4 进程：bus_server / perception / cognition / interaction | 2 进程：yuki 主进程 / model_worker |
| 音频帧、滚动事件、reply 均跨进程传输 | 主链路走内存事件/本地服务；protobuf 与 ZeroMQ 只存在于进程边界 |
| cognition 进程内重推理与实时采集互相抢占 | 重推理全部隔离在 worker，主进程只剩事件处理 |
| 任一层崩溃独立重启 | 主进程/worker 双向独立重启（见“Supervisor 变更”） |

## 主进程内通信模型

主进程不把 BusHub 当作内部消息中枢。新增 `LocalRuntimeBus`，向现有 agent 提供兼容的
`publish/subscribe/respond/request` 表面，但实现完全位于进程内：

文中的 `RemoteBusNode` 是“边界用途”的角色名，代码仍使用完成并发改造后的 `BusNode`，实例变量统一命名
为 `remote_bus`，不额外引入一个继承层次。

- `LocalEventBus`：topic prefix 订阅；每个 handler 使用独立有界队列与单 worker，保持同一 handler
  的事件顺序。慢消费者只丢弃自己的队列，不阻塞音频采集和其他订阅者。
- `LocalServiceRegistry`：`respond(service, handler)` 注册 Python callable，`request()` 在调用线程执行
  本地 handler 并直接返回。耗时调用必须由现有 pipeline worker 或 Gateway 的线程池承载，不能在
  音频回调线程中直接执行。
- `request(..., timeout_ms=...)` 保留兼容参数，但同步 Python handler 无法被安全抢占；它用于记录 deadline
  和超时指标，请求方超时不强杀 handler，与现有 Bus requester 超时后 responder 仍可能继续执行的语义一致。
  真正需要取消的长任务使用显式 job/cancel 协议。
- Registry 使用 thread-local 调用栈检测 `A → ... → A` 的本地服务环并抛 `LocalServiceCycleError`；
  handler 异常统一包装为 `LocalServiceError`，兼容桥再映射成现有 wire error。
- **异常层次兼容（评审 A/H）**：`LocalServiceError` 继承 `BusError`，`request()` 超时抛 `BusTimeoutError`，
  复用现有异常层次——`trigger_call` 等现有 `except (BusError, BusTimeoutError)` 调用点在本地模式下零改动。
- `pause_subscriptions()/resume_subscriptions()` 保留现有 setup 语义；`close()` 唤醒并 join handler worker。
- `RuntimeBus` 暴露 `error_count`、`dropped_count` 与 `health()`，以便 agent 和 app 级健康聚合复用。

主进程内部服务全部注册到 `LocalServiceRegistry`，包括 `frame`、`text`、`memory/*`、`cognition.*`、
`soul.*` 等。模型客户端不经过 `LocalRuntimeBus.request()` 的 prefix 路由，而是显式注入
`RemoteBusNode`；这样从依赖关系上保证只有模型边界会使用跨进程 Bus。

### 本地 payload 与 wire payload

本地分发不受 protobuf Struct 的 JSON 类型限制。高频大载荷使用原生对象：

- `audio/mic`：采集边界创建拥有自身内存的 float32 `numpy.ndarray`，标记为只读后本地传递；不先转 base64，
  也不能引用会被音频驱动复用的 callback buffer。
- `frame`：本地 `FrameStore` 保存 PNG bytes；VLM client 或兼容桥跨进程时才编码 base64。
- 其他低频 payload 首期继续使用现有 dict，降低迁移风险。

`WireCodec` 仅位于进程边界，将原生 payload 转成当前 wire dict，外部观察者看到的 topic、字段与
`proto/yuki.proto` 均不变化。

### 兼容桥

`BusCompatibilityBridge` 使用 `RemoteBusNode` 保留旧的外部入口：

1. 对现有本地服务注册 Bus responder，收到外部请求后转调 `LocalServiceRegistry`。
2. 订阅本地事件并通过独立、有界 mirror queue 发布到 BusHub；内部消费者不订阅镜像，因此不会重复消费。
3. mirror queue 拥塞只增加 bridge 的 dropped 计数，不反压主实时链路。默认镜像现有全部 topic，
   从而保留独立 recorder/debug consumer；后续可按配置缩小前缀范围。
4. Gateway 已在主进程内，直接使用本地 runtime，不通过兼容桥绕行。

旧的 `python -m yuki.perception/cognition/interaction` 入口使用 `WireRuntimeBusAdapter(BusNode)`：adapter
在 agent 边界把原生 payload 编解码为现有 wire dict，使 agent 内部始终只处理同一种本地表示，避免在
AudioCapture/Pipeline 中根据 bus 类型分支。model_worker 是纯边界进程，继续直接使用 RemoteBusNode。

因此，“wire format 不变”是进程边界兼容承诺，不代表主进程内部仍需构造 protobuf 或经过 loopback socket。

## 模型后端抽象（关键接缝）

新增配置 `models.backend`，取值 `local | remote`：

- **`remote`（默认）**：模型在 `model_worker` 进程，主进程通过注入模型客户端的 RemoteBusNode 调用。
- **`local`**：今天的形态——模型内嵌在调用方进程，`python -m yuki.cognition` 等旧入口保留，用于单进程调试。
- **边界（评审 C）**：`yuki.app` 只支持 `remote`（model_worker 为必需进程）；`backend=local` 仅服务旧独立
  入口的调试与回退路径，`yuki.app` 不提供 local 分支。

装配层（`CognitionAssembler` / `InteractionAgent`）按 backend 选择注入"模型对象"或"客户端对象"。
pipeline、LocalRouter、LocalComposer、DecisionHub 的业务调用点保持不变；为支持远端模型，
边界 `RemoteBusNode` responder 调度、`TtsController` 的显式取消钩子以及装配代码按后文修改。

### 接口镜像表

每个模型对象在 `src/yuki/model_client.py` 有一个客户端孪生，实现相同的公开推理接口：

| 模型对象 | 公开接口（现状） | 客户端孪生 | 说明 |
|---|---|---|---|
| `VisualUnderstander` | `understand(image, cache_key)`、`understand_for_question(...)`、`warmup()`、`health()`、`load()/unload()` | `VlmClient` | 图像以 PNG base64 过总线；context 缓存留在 worker 侧（原语义不变） |
| `SpeechRecognizer` | `recognize(samples, sample_rate)`、`warmup()`、`health()` | `SttClient` | 语音段 int16 PCM base64；主进程 `_stt_worker` 线程内阻塞调用 |
| `LocalChatModel` | `generate(messages, max_new_tokens, timeout_ms)`、`warmup()`、`health()` | `LocalChatModelClient` | LocalRouter/LocalComposer 只依赖 `generate()`，注入 client 即零改动 |
| `IndexTTSModel` | `synthesize_stream(text, emotion_vector, ref_audio, lang)`、`warmup()`、`health()` | `TtsClient` | 流式音频走有界 job 队列 + `model/tts_next` long-poll；额外提供 `cancel()` |
| embedding indexer | `embed(texts) -> list[list[float]]` | `EmbeddingClient` | 仅 `memory.vector_enabled` 时启用 |
| `ModelRegistry` | `get_overall_status()`、`get_model_health()`、`preflight()`、`shutdown()` | `RemoteModelRegistry` | 查询 worker 的 `models/*` 服务；新增 operation submit/status/cancel；`shutdown()` 为空操作 |

**VAD 例外**：`FsmnVadBackend` 留在主进程（`SpeechBuffer` 原样）。理由：每 `vad_interval_ms`（默认 400ms）
高频执行、模型极小，跨进程传输 20ms 音频帧不划算。worker 的 registry 不登记 vad。

### 客户端失败语义

客户端调用失败（超时 / `service not found` / 总线错误）时，返回与模型本地失败一致的降级结果：

- `VlmClient.understand` → `{"topic": "", "summary": "", "content_type": "unknown", "key_points": [], "degraded": True, "reason": "model_worker_unavailable"}`（与 `vlm.py` 现有 degrade 形状一致）
- `SttClient.recognize` → 抛 `BusTimeoutError`（pipeline 现有异常路径处理）
- `LocalChatModelClient.generate` → 抛异常（LocalRouter 现有 retry/fail-closed 到 cloud 的路径处理）
- `TtsClient.synthesize_stream` → 抛出后由 TtsController 降级（现有控制台兜底路径）

## 模型生命周期与资源管理

这里的“模型独立进程”指五类本地模型共同位于一个 `model_worker`，不是每个模型各起一个 OS 进程。
supervisor 不管理单个模型；模型级生命周期留在 worker 内，避免为每次 load/unload 重启进程。职责固定分层如下：

| 层级 | 职责 |
|---|---|
| `Supervisor` | 只管理 `model_worker` 进程是否存活、健康失败后的重启与退避 |
| `ModelManager` | 单个模型的加载、卸载、状态转换、显存准入、驱逐与局部恢复；是权威状态源 |
| `ModelInferenceScheduler` | 推理请求的有界排队、优先级、准入、取消与调用 lease |
| `GpuMemoryMonitor` | 采集可用/已用/保留显存，向 ModelManager 提供准入和 OOM 缓解决策输入 |
| `ModelOperationStore` | 保存进程生命周期内的 operation、幂等 key 和限时结果；不承担持久任务职责 |
| `RemoteModelRegistry` | 主进程内的查询/控制客户端，不保存权威模型状态，也不直接持有模型对象 |

### ManagedModelSpec 与 ModelController

worker 为 VLM、STT、local_chat、TTS、embedding 各创建一个 `ModelController`。使用新名字
`ManagedModelSpec`，避免与现有 `cognition.model_registry.ModelSpec/ModelState` 的兼容类型混淆；装配层将现有
loader、unloader 和检查函数连同新策略合并为权威 spec。静态配置与动态状态分离：

```python
@dataclass(frozen=True)
class ManagedModelSpec:
    name: str
    loader: Callable[[], Any]
    unloader: Callable[[Any], None] | None
    health_check: Callable[[], dict] | None
    preflight_check: Callable[[], dict] | None
    dependencies: tuple[str, ...]
    enabled: bool
    manual_unload_allowed: bool
    priority: int
    warmup: bool
    evictable: bool
    pinned: bool
    idle_unload_s: float
    min_residency_s: float
    estimated_vram_mb: int

@dataclass
class ModelRuntimeState:
    state: ModelReadinessState
    active_calls: int
    accepting_calls: bool
    last_used_at: float | None
    failure_count: int
    retry_after: float | None
    last_error_code: str | None
```

模型 readiness 状态机：

```text
disabled

unloaded ──load──► loading ──success──► ready
                     └──failure──► failed ──retry──► loading

ready ──recoverable error──► degraded ──recovered──► ready
ready/degraded ──unload──► draining ──active_calls=0──► unloading ──► unloaded
```

- `busy` 不作为 readiness 状态；并发调用由 `active_calls` 表达，避免状态组合爆炸。
- `evictable` 表示模型实现与策略允许自动卸载；`pinned` 表示当前部署要求常驻，优先级高于 `evictable`，
  但不阻止显式的管理员 unload。disabled 模型不得 load，也不进入调度或驱逐候选。
- 推理开始前从 controller 获取 lease，原子增加 `active_calls`；结束时归还并更新 `last_used_at`。
- `draining` 后 `accepting_calls=false`，拒绝新 lease，取消排队任务，并在 `drain_timeout_s` 内等待 active call。
  超时后请求协作取消；不能安全终止的 CUDA 调用由关闭流程继续等待或升级为 worker 重启，不强杀线程。
- 每个模型的 load/unload 锁与 inference lock 独立；跨模型显存准入由 ModelManager 的全局 memory lock 串行化。
- **调用统计兼容（评审 B）**：vlm/stt/vad/local model 现有 `set_model_registry(registry, name)` 与
  `_model_call_tracker() → registry.track_call(...)` 钩子改挂 ModelManager：ModelManager 提供 `track_call`
  兼容表面（成功/失败/延迟统计并入 scheduler lease 记录），模型对象代码零改动。
- worker 启动时校验依赖图无环。load 按拓扑顺序先加载依赖；卸载一个仍有已加载 dependent 的模型时，
  沿依赖闭包先 drain 并逆拓扑卸载 dependent，以保持现有级联语义。operation 对依赖闭包按固定名称顺序取锁，
  不允许两个交叉 reload 形成死锁。

### 默认调度策略

| 模型 | 优先级 | warmup | 可驱逐 | 固定 | 默认定位 |
|---|---:|---|---|---|---|
| `local_chat` | 100 | 是 | 否 | 是 | 路由与短回复，交互关键路径 |
| `stt` | 90 | 是 | 否 | 是 | 用户语音，交互关键路径 |
| `tts` | 80 | 是 | 是 | 否 | 回复播放，流式且可取消 |
| `vlm` | 30 | 否 | 是 | 否 | 深理解，允许排队或降级 |
| `embedding` | 10 | 否 | 是 | 否 | 后台索引，可延迟执行 |

Scheduler 至少包含 `interactive`、`background` 两个有界推理队列；TTS chunk 等待仍使用 Bus 的 `stream`
lane，但实际 TTS 推理占用 interactive 调度 lease。默认 GPU 主推理并发为 1。优先级只决定尚未开始的下一个
任务，不能抢占正在执行的 CUDA kernel；TTS 依靠生成器 chunk 边界检查 cancellation event，不宣称硬抢占。

### 显存准入与驱逐

加载模型前执行：

1. 根据 `estimated_vram_mb`、GpuMemoryMonitor 快照和安全余量判断是否可准入。
2. 显存不足时，从 `evictable=true`、`pinned=false`、`active_calls=0` 的 ready/degraded 模型中选择候选；
   正在加载的目标及其依赖闭包、任何 active lease 所需的依赖均不得驱逐。
3. 候选按优先级升序、`last_used_at` 最早优先驱逐；每次卸载后重新采样显存，直到满足准入或无候选。
4. 仍不足则拒绝加载，模型进入 degraded/failed，并返回 `insufficient_vram`，不得为了加载后台模型驱逐
   local_chat 或 STT。
5. idle reaper 定期卸载超过 `idle_unload_s` 的可驱逐模型；`idle_unload_s=0` 表示不因空闲卸载。

推理发生 CUDA OOM 时，当前任务失败并释放 lease，ModelManager 执行 `empty_cache` + 候选驱逐，最多自动重试
`oom_retry` 次。重试仍 OOM 时打开该模型的 circuit breaker，在 `circuit_breaker_s` 内快速降级。CUDA illegal
memory access、device lost、无法同步等表明 context 可能损坏的错误标记为 `worker_fatal`，健康检查返回
unhealthy，由 supervisor 重启整个 worker；普通模型加载/推理失败不得触发进程重启。OOM 紧急缓解可忽略
`min_residency_s`，但仍不得驱逐 pinned、不可驱逐或持有 active lease 的模型。

### 异步管理操作

load/unload/reload/preflight/relieve_memory_pressure 可能超过普通 Bus timeout，权威管理 API 使用异步
operation：

```text
queued → running → succeeded
              ├──► failed
              └──► cancelled
```

- submit 请求必须携带客户端生成的 `idempotency_key`；同 key 重试返回同一 operation_id。
- 幂等映射的作用域是单次 worker 进程生命周期；load/unload 必须按目标状态幂等。worker 重启后 reload
  可能安全地重复执行一次，因此实现必须保证重复 unload/load 不泄漏 handle，也不破坏依赖计数。
- 同一模型的管理 operation 串行执行；不同模型仍受全局 memory lock 与 scheduler lease 约束。
- 只有 queued operation 可以保证取消；running operation 进入协作取消/安全点，不强制终止加载线程。
- operation 完成后保留 `operation_ttl_s`，过期清理；状态结果包含 action、model、timestamps、error_code，
  不返回异常堆栈或本地模型路径。
- worker 关闭时先停止接受新 inference/operation，取消 queued 项，drain active calls，停止 TTS，
  再按加载逆序卸载模型并关闭 RemoteBusNode。

现有 `models/unload`、`models/reload`、`models/preflight`、`models/relieve_memory_pressure` 的 service 名和
payload 保持不变，作为兼容 wrapper：内部提交 operation 并等待原有调用语义。**wrapper 等待上限对齐旧客户端
超时（默认约 2s，评审 E）**：超时后返回 `{ok: false, operation_id, reason: "operation_pending"}`，不无限占用
work lane；调用方（如旧管理 CLI）转查 `models/operations/status` 获得最终结果。新的 RemoteModelRegistry 和
管理界面直接使用异步 operation 服务，避免占用 responder work lane。

### 管理可观测性

- `models/health` 的每个模型暴露 readiness、`active_calls`、是否接受新调用、最近使用时间、失败次数、
  熔断截止时间和最后一个稳定错误码；不得返回模型路径或原始异常文本。
- `health/model_worker.components` 至少包含 manager loop、scheduler、operation worker、GPU runtime 和各模型，
  并报告 interactive/background 队列深度、最老等待时间及 operation backlog。
- 结构化日志统一携带 `operation_id/model/action/reason/from_state/to_state`；记录 load/unload 时长、推理排队
  与执行时长、驱逐原因、OOM/重试/熔断次数和 drain 超时，便于区分容量问题与模型实现故障。
- 管理状态读取只获取加锁快照，不调用模型 health callback，不采样 GPU，也不等待 operation；慢采样由后台
  刷新缓存，保证 control lane 延迟可预测。

## 边界 BusNode responder 并发模型（前置改造）

当前 `BusNode._dealer_loop` 在唯一的 DEALER I/O 线程中同步调用 responder handler。该行为不能直接用于
进程边界：model worker 的长推理会阻塞健康探活、取消请求、服务续租与其他模型调用；主进程兼容桥的
外部 `cognition.chat` 请求也可能转入本地逻辑并等待远端模型响应。虽然主进程内部主链路已经不使用 Bus，
边界 responder 仍必须允许嵌套 request，并保证控制面不被长任务阻塞。

因此，创建 `model_worker`、`RemoteBusNode` 与兼容桥之前，先完成以下总线改造：

1. DEALER socket 仍只由 `_dealer_loop` 线程读写；该线程不执行用户 handler。
2. 收到服务请求后，将 handler 投递到有界 executor；完成结果写入线程安全的 response outbox，
   再由 `_dealer_loop` 发送。handler 内允许同步调用同一 BusNode 的 `request()`。
3. responder 分为三个执行 lane：
   - `control`：`health/*`、`models/health`、`models/list`、`models/policy`、`models/operations/*`、
     `model/tts_cancel`；
     保留独立线程与队列，handler 只能查询或向 management queue 入队，不得直接加载/卸载模型。
   - `work`：普通业务服务、模型推理以及 `models/preflight/unload/reload/relieve_memory_pressure`；
     队列有上限，满时立即返回 `server busy`，不无限堆积。
   - `stream`：`model/tts_next` long-poll；使用独立有界 executor，避免等待音频 chunk 占满 work/control。
4. `respond(service, handler, *, lane="work")` 增加可选 lane；现有调用保持默认行为，HealthReporter
   显式使用 `control`。关闭时先停止接收新工作，再取消/等待 executor，最后关闭 socket。
5. model worker 在 work lane 之上使用 `ModelInferenceScheduler` 的 interactive/background 队列与
   ModelController lease；VLM/STT/embedding/TTS 的实际执行不得占用 control lane。

该改造是两进程迁移的硬前置，不允许以“handler 内另起线程但 I/O 线程等待结果”的方式替代。

## 总线服务契约

### 复用（服务名与既有字段语义兼容，宿主从 cognition 移到 worker）

保留 `model_service.py` 的服务名与既有字段语义：`models/health`、`models/unload`、`models/reload`、
`models/preflight`、`models/list`、`models/relieve_memory_pressure`。实现改为委托 `ModelManager`；其中变更型
服务作为兼容 wrapper，提交异步 operation 后等待并映射回旧响应。`models/health` 可增加
`active_calls/accepting_calls/last_used_at/retry_after` 等可选字段，但已有字段不删除、不改义。主进程不再注册
这些服务。

内部 readiness 写入新增的 `runtime_state`。旧 `state` 字段继续只返回现有枚举值并做兼容映射：
`disabled/unloaded → not_loaded`、`loading → loading`、`ready/draining/unloading → loaded`、
`degraded → degraded`、`failed → error`。调用方需要精确管理状态时读取 `runtime_state`。

### 新增异步管理服务（worker 注册）

| 服务 | 请求 payload | 响应 payload | lane |
|---|---|---|---|
| `models/operations/submit` | `{idempotency_key, action, model?, reason?}` | `{operation_id, accepted}` | control（仅入队） |
| `models/operations/status` | `{operation_id}` | `{state, action, model?, created_at, started_at?, finished_at?, result?, error_code?}` | control |
| `models/operations/cancel` | `{operation_id}` | `{cancel_requested, state}` | control |
| `models/policy` | `{model?}` | `{policies}` | control，只读 |

`action` 只允许 `load | unload | reload | preflight | relieve_memory_pressure`；model 名必须来自静态 catalog，
不得允许请求方提交任意模型 ID、路径或 Python factory。实际 operation 由 worker 内独立 management queue 执行，
control handler 只做校验、查询和入队。load/unload/reload 必须指定 model；preflight 的 model 可省略以检查全部；
relieve_memory_pressure 不接受 model。未知 operation_id 统一返回 `operation_not_found`，不以空成功掩盖 TTL 或
worker 重启造成的状态丢失。

### 新增推理服务（worker 注册）

| 服务 | 请求 payload | 响应 payload |
|---|---|---|
| `model/vlm_understand` | `{image_png_b64, cache_key?}` | `{context: dict}`（含 degraded 标志） |
| `model/vlm_understand_question` | `{image_png_b64, question, cache_key?}` | `{context: dict}` |
| `model/stt_recognize` | `{samples_b64, sample_rate, encoding: "pcm_s16le"}` | `{text}` |
| `model/local_generate` | `{messages, max_new_tokens, timeout_ms}` | `{text}` |
| `model/tts_synthesize` | `{job_id, text, emotion_vector?, ref_audio?, lang?}` | `{job_id, accepted}` |
| `model/tts_next` | `{job_id, after_seq, wait_ms}` | `{ready, seq?, pcm_b64?, done, error?}` |
| `model/tts_cancel` | `{job_id}` | `{}` |
| `model/embed`（可选） | `{texts}` | `{vectors}` |

STT 编码固定为 little-endian signed int16：客户端将现有 float32 `[-1, 1]` 样本 clip 后乘 32767，
worker 解码并还原为 float32。最长语音段原始大小 10s × 16kHz × 2B = 320KB，base64 后约 427KB；
VLM 截屏 PNG 与现有 `frame` 服务同量级，均远小于 `bus.max_msg_size`（默认 10MB）。

**STT 超时**：`BusNode.request` 默认 timeout 2000ms 不够（10s 音频 + 推理）。`SttClient` 按样本时长计算
`timeout_ms = samples_duration_s * 1000 + 5000`，并保留现有 `stt.retry_window_s` 语义（由 worker 侧实现）。

## TTS 流式协议

`IndexTTSModel.synthesize_stream` 目前是流式生成器（`stream_return=True`，边合成边 yield）。
一次性 REP 返回全部音频会牺牲起播延迟。PUB/SUB 对尚未建立 job mailbox 的客户端没有重放能力，
worker 若在 `{job_id}` REP 到达前发布首块会产生不可恢复的丢包。因此采用**有界 job 队列 + long-poll**，
不使用 TTS chunk 主题：

```
主进程 TtsClient                         model_worker
  │  本地生成 job_id                      │
  │  REQ model/tts_synthesize ──────────►│ 建立有界 job 队列，起合成线程
  │  ◄──── {job_id, accepted} REP         │ 逐块写入队列并分配递增 seq
  │  REQ model/tts_next(after_seq) ─────►│ 最多等待 wait_ms
  │  ◄──── {ready, seq, pcm_b64, done}    │
  │  synthesize_stream() yield pcm        │
  │  （重复 next，直到 done）              │
```

- `TtsController` 仍由现有 worker 线程驱动生成器，但增加可选的模型取消钩子：当 transition 被替换、
  `cancel(reply_id)`、`stop()` 或 `shutdown()` 取消正在处理的 job 时，调用 `TtsClient.cancel()`。
- `TtsClient` 线程安全地保存当前 generator 对应的 job_id；`cancel()` 发送
  `model/tts_cancel {job_id}`。worker 设置 cancellation event、唤醒正在 long-poll 的 `tts_next`，
  停止合成并释放缓冲。
- 防串扰：job_id 由客户端生成且必须唯一；worker 校验归属；客户端严格校验 seq 连续递增。
- `model/tts_next` 具备重试幂等性：worker 只在下一次请求携带更大的 `after_seq` 时确认并丢弃旧 chunk；
  若 REP 丢失，客户端以原 `after_seq` 重试会得到同一 seq，而不会跳过音频。
- 背压：每 job 队列按音频时长或 chunk 数设上限；消费者过慢时中止 job 并返回 `client_slow`，
  不允许 worker 内存无界增长。完成、取消或 60s 无访问均清理 job。
- `model/tts_next` 的 `wait_ms` 采用短 long-poll（例如 2s）；暂无 chunk 时返回 `ready=false`，
  客户端使用 `timeout_ms = wait_ms + 1000` 并在返回后检查取消状态，不把正常等待当成 TTS 失败。
- `audio/tts_ref`（AEC 参考信号）主题保持不变：worker 不参与，主进程 interaction 照旧发布。

## 主进程运行时与生命周期

新增包 `src/yuki/app/`：

- `main()`：`Config.from_env()` → 创建 `BusHub` → 创建边界 `RemoteBusNode`
  → 创建 `LocalRuntimeBus` 与 `BusCompatibilityBridge` → 创建唯一 `ShutdownManager`
  → 构造三个 agent（必须注入 `bus=local_runtime_bus, shutdown=shared_shutdown`；模型客户端单独注入
  `remote_bus`）→ 暂停本地订阅 → 依次 `setup()` → 启动兼容桥与 app 级健康聚合
  → 恢复本地订阅 → 每 agent 的 `loop()` 跑 daemon 线程 → `gateway.enabled` 时以本地 runtime 启动 Gateway。
- app 创建唯一 `HealthReporter(remote_bus, process="yuki")`，通过进程边界响应 supervisor，
  不启动三个 agent 各自的 HealthReporter。
  将 `agent.health_components()` 以 `perception.*` / `cognition.*` / `interaction.*` 命名空间注册，
  并加入 `bus_hub`、`local_runtime_bus`、`remote_bus`、mirror queue、loop 线程存活状态与 Gateway
  （启用时）。`BusHub` 增加公开、无总线回调的 `health_snapshot()`，避免健康 handler 再递归探活。
- `health/yuki` 只表示主进程自身可服务；远端模型健康保留在 `health/model_worker`，主进程不得因为
  worker 暂时不可用而报告自身 unhealthy。`models.backend == "remote"` 时，app 聚合器不把
  `cognition.vlm`、`cognition.stt`、`cognition.models`、`interaction.tts` 注册为主进程硬健康项；如需展示，
  只读取后台刷新得到的缓存快照并标记 degraded，健康 handler 内不发远端请求。
- `health/model_worker` 的进程级判定只检查 control lane、management loop、scheduler 与 CUDA runtime 是否
  可服务。单个模型的 `failed/degraded` 写入 components 和 `models/health`，但不直接令进程 unhealthy；只有
  `worker_fatal` 或管理基础设施失效才触发 supervisor 重启。
- 关闭顺序：信号 → `shared_shutdown.request_shutdown()` → 停止接收 Gateway 和兼容桥新请求
  → join 各 loop 线程 → 逆序 `teardown()` → `app_health.stop()` → `bridge.close()`
  → `local_runtime_bus.close()` → `shared_shutdown.run_cleanups()` → `remote_bus.close()` → `hub.close()`。
  每次 join 有明确超时；超时记录线程名并继续清理，不能无限挂起。
- **不能直接复用 `ProcessAgent.run()`**（它内部注册信号、start health、close bus，均为进程级操作）。
  `app/main.py` 协调器固定采用显式 `setup/loop/teardown`，不新增 `run_loop_only()`，也不启动各 agent
  自带的 HealthReporter；所有进程级资源只由 app 协调器拥有和关闭。
- `ProcessAgent` 的 bus 类型从具体 `BusNode` 收窄为项目内 `RuntimeBusProtocol`；旧入口注入
  `WireRuntimeBusAdapter(BusNode)`，`yuki.app` 注入 LocalRuntimeBus。agent 不得通过类型判断依赖具体实现。
- 信号：仅主线程注册（Python 限制）；`--trigger-after` 参数由 supervisor 传入主进程，interaction 现有逻辑照常生效。
- 主进程内部关闭、事件背压和 handler 异常由 LocalRuntimeBus 独立统计；边界 BusNode 的 responder
  可重入性仍必须先按“边界 BusNode responder 并发模型”一节改造。

## Supervisor 变更

`src/yuki/supervisor/main.py`：

```python
CHILDREN = [
    ("yuki", [sys.executable, "-m", "yuki.app"]),
    ("model_worker", [sys.executable, "-m", "yuki.model_worker"]),
]
```

- supervisor 明确配置 `bus_host="yuki"`，`tick` 按依赖顺序探活：
  1. 若 yuki 子进程退出，安排重启并将 bus 标为 unavailable；本轮跳过所有依赖总线的健康请求。
  2. yuki 进程存活时先请求 BusHub 内置 `health/bus_server`；失败或 unhealthy 只重启 yuki，并跳过 worker 探活。
     **语义注记（评审 b3 确认）**：`bus_health.healthy=false`（hub 明确报不健康）时立即重启 yuki、无宽限；
     而**探活失败**（hub 无响应/超时）在 yuki 启动宽限 `startup_grace_s` 内不重启——yuki 自身是 hub 宿主，
     启动期 hub 尚未就绪属正常状态，不应触发自杀式重启。两类情况分开处理。
  3. BusHub 健康后请求 `health/yuki`，再请求 `health/model_worker`。
  4. BusHub 从不可用恢复时记录 `bus_recovered_at`。在配置的有效服务重注册宽限期内，worker 进程仍存活但
     `health/model_worker` 尚未注册只记 pending，不重启 worker。
- 因此 `src/yuki/supervisor/__init__.py::tick` 必须修改；不能继续依赖“子进程名等于 bus_server”的特判。
- 重启语义：worker 崩溃 → supervisor 按退避重启 worker；期间主进程各客户端按“客户端失败语义”降级，
  worker 重启后主进程自动恢复。主进程崩溃 → 重启主进程，worker 不受影响；BusHub 恢复后 worker
  最迟在下一次 `register_interval` 周期重新 REGISTER。
- **核心收益保留：模型进程与交互进程互相独立重启**，与现状等价的容错面。

## 降级与容错

| 故障 | 行为 |
|---|---|
| worker 崩溃/未启动 | VLM 深推降级（快档文本理解继续）、STT 无识别（唤醒后超时回落，现状同款）、TTS 控制台兜底、本地脑 fail-closed 到 cloud（LocalRouter 现有路径） |
| 主进程崩溃 | supervisor 仅重启主进程；总线恢复前跳过 worker 探活，worker 继续常驻并在 BusNode 周期 REGISTER 后恢复服务 |
| worker 内模型 OOM | 当前请求失败并释放 lease；ModelManager 清缓存、按策略驱逐并有限重试；模型级失败进入熔断，只有 CUDA context 疑似损坏才标记 `worker_fatal` |
| 主进程内单 agent 异常 | 与现状不同：无法单独重启（接受项）。主进程为纯事件处理，崩溃面远小于推理；由 supervisor 整体重启 |

## 配置变更

`src/yuki/config.py` 新增：

```python
class ModelPolicyConfig(BaseModel):
    priority: int = Field(50, ge=0, le=100)
    warmup: bool = False
    evictable: bool = True
    pinned: bool = False
    idle_unload_s: float = Field(0.0, ge=0.0)
    min_residency_s: float = Field(30.0, ge=0.0)
    estimated_vram_mb: int = Field(0, ge=0)

class ModelsConfig(BaseModel):
    backend: Literal["local", "remote"] = "remote"
    gpu_max_concurrency: int = Field(1, ge=1)
    interactive_queue_size: int = Field(32, ge=1)
    background_queue_size: int = Field(16, ge=1)
    drain_timeout_s: float = Field(10.0, ge=0.1)
    operation_ttl_s: float = Field(300.0, ge=1.0)
    circuit_breaker_s: float = Field(30.0, ge=0.1)
    oom_retry: int = Field(1, ge=0, le=2)
    vram_safety_margin_mb: int = Field(512, ge=0)
    vram_hysteresis_mb: int = Field(256, ge=0)
    policies: dict[str, ModelPolicyConfig] = Field(default_factory=default_model_policies)

# default_model_policies() 必须与"默认调度策略"表一致（评审 G）：
#   local_chat: priority=100, warmup=True,  evictable=False, pinned=True
#   stt:        priority=90,  warmup=True,  evictable=False, pinned=True
#   tts:        priority=80,  warmup=True,  evictable=True,  pinned=False
#   vlm:        priority=30,  warmup=False, evictable=True,  pinned=False
#   embedding:  priority=10,  warmup=False, evictable=True,  pinned=False
# 其余字段（idle_unload_s/min_residency_s/estimated_vram_mb）取 ModelPolicyConfig 默认值。

# Config 增加字段：models: ModelsConfig = Field(default_factory=ModelsConfig)

class RuntimeBusConfig(BaseModel):
    subscriber_queue_size: int = Field(256, ge=1)
    mirror_queue_size: int = Field(1024, ge=1)
    mirror_topic_prefixes: list[str] = Field(default_factory=lambda: [""])

# Config 增加字段：runtime_bus: RuntimeBusConfig = Field(default_factory=RuntimeBusConfig)

# BusConfig 增加：
register_interval_s: float = Field(10.0, ge=1.0)

# SupervisorConfig 增加：
bus_recovery_grace_s: float = Field(20.0, ge=1.0)
```

`local` 模式完全复现现状（旧入口 `python -m yuki.cognition` 等继续可用，便于调试与回退）。
remote 模式下 `models.policies` 是 worker 运行策略的权威配置；模型 ID、路径、device、enabled 等仍来自
现有 `vlm/stt/tts/local_brain/memory` 分区。policy key 只允许静态 catalog 中的
`vlm | stt | local_chat | tts | embedding`，未知 key 在配置加载时拒绝。旧分区中的 warmup 开关仅继续服务
local/standalone 兼容路径，remote worker 使用上表默认策略或显式 `models.policies` 覆盖。
`pinned` 是对自动驱逐的部署覆盖，可与“模型本身可驱逐”的 `evictable=true` 同时存在。新加载模型在
`min_residency_s` 内不因普通准入被再次驱逐；准入目标额外包含 `vram_hysteresis_mb`，为峰值波动提供迟滞。
`estimated_vram_mb=0` 表示采用 worker 静态 catalog 的保守估算，不表示该模型不占显存；显式配置正整数时
覆盖 catalog 值。默认 catalog 当前为 VLM 5120、STT 1536、local_chat 2048、TTS 2048、embedding 1024 MB。
`BusNode` 构造统一使用 `bus.register_interval_s`。supervisor 实际采用的恢复宽限期为
`max(supervisor.bus_recovery_grace_s, 2 * bus.register_interval_s, supervisor.startup_grace_s)`。
`runtime_bus.mirror_topic_prefixes=[""]` 表示兼容镜像全部 topic；设置为空列表可关闭事件镜像但不影响
服务桥和模型通信。worker 读取同一份 `config.yaml`（`bus`/`logging`/`health`/`vlm`/`stt`/`tts`/
`local_brain`/`memory` 分区）。

## 文件变更清单

**新增**
- `src/yuki/runtime_bus.py` — `RuntimeBusProtocol`、`LocalEventBus`、`LocalServiceRegistry`、`LocalRuntimeBus`
- `src/yuki/bus_bridge.py` — `WireCodec`、`WireRuntimeBusAdapter`、`BusCompatibilityBridge`，
  负责 standalone 编解码、服务代理与异步事件镜像
- `src/yuki/model_worker/__init__.py`、`__main__.py`
- `src/yuki/model_worker/agent.py` — `ModelWorkerAgent(ProcessAgent)`，`name="model_worker"`，`register_health=True`
- `src/yuki/model_worker/controller.py` — `ManagedModelSpec`、`ModelReadinessState`、调用 lease、draining 与 circuit breaker
- `src/yuki/model_worker/manager.py` — `ModelManager` 权威 catalog、显存准入、LRU 驱逐、idle reaper 与 OOM 恢复
- `src/yuki/model_worker/operations.py` — 幂等异步管理 operation、串行化、取消与 TTL 清理
- `src/yuki/model_worker/assembly.py` — 模型装配：复用 `cognition/assembly.py` 的模型对象构建
  （`_build_vlm/_build_stt/_build_local_brain`）与 loader/unloader/health 提取；**注册改走 ManagedModelSpec
  catalog，不注册旧 ModelSpec（评审 D）**；新增 `_build_tts`；注册 `models/*` wrapper 与新增 `model/*` 服务
- `src/yuki/model_worker/scheduler.py` — interactive/background 有界优先级队列、GPU 并发准入与取消
- `src/yuki/app/__init__.py`、`__main__.py`、`main.py` — 主进程装配、app 级健康聚合与生命周期协调
- `src/yuki/model_client.py` — `VlmClient` / `SttClient` / `LocalChatModelClient` / `TtsClient` / `EmbeddingClient` / `RemoteModelRegistry`
- 测试：`tests/test_runtime_bus.py`、`tests/test_bus_bridge.py`、`tests/test_model_client.py`、
  `tests/model_worker/`、`tests/app/`

**修改**
- `src/yuki/bus.py` — responder handler 移出 DEALER I/O 线程；control/work/stream lane、response outbox、
  有界队列与关闭语义；`BusHub.health_snapshot()`
- `src/yuki/health.py` — 健康服务注册到 control lane
- `src/yuki/config.py` — `ModelsConfig`、`RuntimeBusConfig`、Bus/Supervisor 恢复配置
- `src/yuki/cognition/model_registry.py` — 保留现有 `ModelSpec/ModelState/ModelRegistry` 导入与本地模式接口；
  remote 模式的权威状态迁移到 ModelManager，并提供 legacy `state` 映射；RemoteModelRegistry 增加
  operation submit/status/cancel
- `src/yuki/cognition/model_service.py` — 旧 `models/*` 兼容 wrapper + 新异步 operation/policy 服务注册
- `src/yuki/process.py` — bus 注解改为 `RuntimeBusProtocol`；旧入口默认构造
  `WireRuntimeBusAdapter(BusNode)`，生命周期不依赖具体实现
- `src/yuki/perception/audio.py`、`capture.py` — 主进程本地路径传原生 samples/PNG bytes，wire 编码移到边界
- `src/yuki/perception/wake_word.py`、`src/yuki/cognition/pipeline.py`、`frame_client.py` — 接受本地原生 payload；
  旧 standalone/wire payload 仍由兼容 codec 支持
- `src/yuki/cognition/assembly.py` — `models.backend == "remote"` 时跳过本地模型构建，注入客户端与 `RemoteModelRegistry`；`local_brain` 支持注入（`local_router/local_composer` 参数）
- `src/yuki/cognition/agent.py` — 扩展注入参数（local_brain / 客户端），`_health_models` 兼容 remote registry
- `src/yuki/interaction/agent.py` — 新增 `tts_model` 注入参数（默认 `IndexTTSModel(config.tts)` 不变）
- `src/yuki/interaction/tts_controller.py` — 取消/替换/停止时调用模型的可选 `cancel()` 钩子
- `src/yuki/supervisor/__init__.py`、`main.py` — `CHILDREN` 2 项、bus_host 探活依赖与总线恢复宽限期
- `src/yuki/bus_server/agent.py` / `gateway.py` — Gateway 装配支持注入本地 runtime，主进程内不绕 BusHub
- `tests/test_e2e.py`、`tests/test_supervisor_main.py` — 进程清单与断言更新
- `README.md` — 架构图与运行说明

## 迁移步骤（实施顺序）

1. **本地 runtime**：新增 `RuntimeBusProtocol`、LocalEventBus/LocalServiceRegistry/LocalRuntimeBus；
   用单元测试固定顺序、背压、关闭与异常隔离语义，但暂不切换 agent。
2. **边界 BusNode 前置改造**：实现异步 responder、control/work/stream lane、嵌套 request、
   response outbox 与关闭语义；新增回归测试后再继续迁移。
3. **config**：新增完整 `ModelsConfig` policy、`RuntimeBusConfig` 与恢复配置。
4. **模型管理核心**：先以 fake 模型实现 ModelController/ModelManager/ModelOperationStore/Scheduler，
   固定状态机、lease、draining、显存驱逐、幂等 operation 与错误分类。
5. **worker 侧**：`src/yuki/model_worker/` 装配管理核心 + 五类模型 + 旧 `models/*` wrapper +
   新 operation/policy 与 `model/*` 推理服务；`python -m yuki.model_worker` 可独立启动。
6. **客户端**：`src/yuki/model_client.py` 各客户端 + `RemoteModelRegistry`（含异步模型管理、STT 动态超时、
   TTS long-poll 与显式取消）。
7. **本地 payload 与兼容桥**：音频/帧切换到本地原生 payload；完成 WireCodec、
   WireRuntimeBusAdapter、服务代理与异步事件镜像，用契约测试证明外部 wire payload 不变，
   并保持三个旧入口可独立运行。
8. **cognition 装配分支**：backend=remote 时将 RemoteBusNode 仅注入模型客户端；`local` 分支不动。
9. **interaction 注入**：`tts_model` 参数 + `TtsClient`，补 `TtsController` cancel 钩子。
10. **主进程入口**：`src/yuki/app/` 生命周期协调（BusHub + RemoteBusNode + LocalRuntimeBus + 兼容桥、
   shared shutdown、app 级 `health/yuki`、三 agent 线程、本地 Gateway）。
11. **supervisor**：`CHILDREN` 2 项，重写 bus_host 探活依赖、恢复宽限期与 worker_fatal 处理。
12. **e2e 更新**：spawn supervisor 断言两进程、`--trigger-after` 闭环、worker 强杀后自动恢复、
   主进程强杀后 worker PID 不变且服务重新注册。
13. **默认值落定**：`backend` 默认 `remote`；旧入口保留为调试路径。
14. **文档**：README 架构图、本设计归档。

## 测试计划

- 单元（bus）：responder handler 不在 DEALER I/O 线程执行；handler 内通过同一 BusNode 发起嵌套 request
  可成功返回；长 work handler 运行时 `health/*` control 请求仍在超时内响应；work 队列满返回
  `server busy`；TTS long-poll 不占用 work/control；关闭时无 socket 跨线程访问。
- 单元（local runtime）：同一 handler 保序；不同 handler 互不阻塞；单个慢消费者队列满只丢自己的事件；
  本地 request 不触发 RemoteBusNode；服务调用环被拒绝；异常映射与 deadline 指标正确；pause/resume、
  异常计数与 close/join 行为确定。
- 单元（compatibility bridge）：原生音频/PNG 只在镜像或外部服务响应时编码；wire topic、字段与旧契约一致；
  WireRuntimeBusAdapter 编解码 round-trip；mirror queue 满不会阻塞 LocalEventBus；外部 responder
  可嵌套调用远端模型；**worker 下线/远端服务不可用期间（评审 F）：本地事件流与本地服务不受影响，
  桥只增加 dropped 计数，不阻塞主链路**。
- 单元（model client）：各客户端（FakeBus 注入，断言请求 payload 与降级语义）；`RemoteModelRegistry`；
  TTS long-poll（job_id/seq 校验、丢失 REP 后同 seq 重放、`ready=false`、done、显式 cancel、
  慢消费者背压与僵尸清理）。
- 单元（模型管理）：覆盖所有合法/非法状态迁移；推理 lease 的 `active_calls` 增减与异常释放；`draining`
  拒绝新 lease、等待归零及超时恢复；同一模型的 operation 串行化；幂等 key 重放；queued/running 取消语义；
  operation TTL 清理；health/status 只读缓存快照且不执行模型 callback/GPU 采样；旧
  `models/unload/reload/preflight/relieve_memory_pressure` wrapper 的 payload 与结果兼容；依赖图环检测、拓扑加载、
  dependent 逆序 drain/unload 与交叉 reload 无死锁。
- 单元（调度与显存）：交互队列优先于后台队列且两者均有界；主 GPU 默认并发 1；准入计算包含安全余量；
  驱逐候选必须同时满足 `evictable && !pinned && active_calls == 0`，并按低优先级、最久未使用排序；默认策略下
  local_chat/STT 永不被自动驱逐；OOM 仅重试配置次数并触发模型级熔断，疑似 CUDA context 损坏才升级为
  `worker_fatal`。
- 集成（`tests/model_worker/`、`tests/app/`）：`ModelWorkerAgent` + 假模型装配；主进程三 agent 线程化
  setup/teardown 顺序；三个 agent 共享同一 ShutdownManager；`health/yuki` 聚合；关闭/断开 RemoteBusNode 时
  本地 audio → cognition → interaction 事件仍可流转；外部 `cognition.chat` 兼容 responder 调用远端模型不死锁；
  长 VLM 推理期间 `health/model_worker`、operation status 与 TTS cancel 可达；worker 关闭时先停止准入，等待
  lease drain，再按逆加载顺序卸载；drain 超时不会让 control lane 失联。
- e2e（`-m e2e`）：supervisor 拉起 2 进程 → `health/yuki`、`health/model_worker` 探活 →
  `--trigger-after` 语音闭环（可降级为 TTS 兜底断言）→ 强杀 worker → 断言主进程降级、supervisor 重启、
  恢复后 `models/health` 可达；另强杀 yuki → 断言 worker PID 不变 → BusHub 恢复并等待 REGISTER
  → `health/model_worker` 与模型服务重新可达；注入可恢复 OOM 验证驱逐与单次重试，注入 context-fatal
  错误验证 worker unhealthy 并由 supervisor 重启。
- 行为等价：`backend=local` 下现有测试集全部保持通过。

## 风险与已知限制

- **优先级不是 GPU 抢占**：本地脑 router 的 `timeout_ms` 默认 150ms，若 VLM 已进入 CUDA kernel，后到的
  local_chat 即使位于交互队列也可能超时并 fail-closed 到 cloud。需记录排队/推理耗时并在目标硬件压测；
  必要时限制 VLM 工作单元、拆分设备或调整超时，不能把 CUDA stream 当作硬抢占保证。
- **显存估算可能失真**：`estimated_vram_mb` 无法完整预测峰值、碎片和后端缓存。准入必须保留安全余量并用
  实测峰值校准；连续 load/evict 需有最短驻留时间和迟滞，防止模型抖动。
- **drain 不能中止原生 kernel**：running operation 的取消只是协作式；超时后模型进入 `failed` 或 worker
  进入不健康状态，不能在线程仍使用模型时强制 unload。只有明确判定 context 损坏才上报 `worker_fatal`，
  否则频繁重启会放大瞬时 OOM。
- **异步 operation 状态仅在 worker 内**：worker 重启后未完成 operation 统一视为失败；进程内幂等 key
  映射也会丢失，调用方应按期望目标状态重新提交；load/unload 会幂等收敛，reload 允许安全地重复一次。
  已完成结果只保留 `operation_ttl_s`，它不是持久任务系统。
- **多个模型共享 worker 故障域**：单模型 Python 异常会被 controller 隔离，但 native crash、进程 abort 或
  CUDA context 损坏会让五类模型同时短暂不可用并整体重启。这是“两进程”目标的明确取舍；若实测某后端
  经常导致进程级崩溃，应另立 ADR 将该后端拆为专属 worker，而不是在本方案内隐式生成子进程。
- **TTS long-poll 为新增契约**：需防 job_id 串扰、慢消费者、重复 next 与僵尸 job，并验证取消能唤醒等待。
- **BusNode 行为变化面较大**：异步 responder 会改变 handler 的并发时序；默认 work lane 必须有界，
  边界服务需做线程安全审计，并以现有 bus fault tests 加嵌套请求/关闭竞态回归覆盖。
- **本地语义与 wire 语义分叉**：原生 payload 和直接 service call 会暴露此前被序列化复制掩盖的共享可变对象；
  本地事件 payload 必须视为只读，必要时在发布处冻结或复制，并补 handler 间隔离测试。
- **兼容镜像成本**：默认镜像全部 topic 会继续消耗编码 CPU，但它位于独立队列，不是主链路依赖；
  实测后可将默认前缀缩小到 `event/`，需要原始音频的调试工具显式开启 `audio/`。
- **主进程崩溃面**：三 agent 共享一个进程，无法单层重启（接受项，见“降级与容错”）。
- **GIL**：主进程内事件 handler 与兼容镜像编码仍受 GIL 影响，但模型重 CPU 在 worker；通过本地队列延迟、
  mirror dropped_count 与 e2e 观测。

## 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 单主进程 + model_worker 双进程 | 实时链路取消跨进程边界；重推理（最脆弱组件）保留独立重启 |
| `models.backend: local/remote` 开关 | local 模式 = 现状，迁移可增量、可回退，旧入口用于调试 |
| 客户端实现同名推理接口 | pipeline / LocalRouter / LocalComposer / DecisionHub 不改；TtsController 仅增加通用 cancel 钩子 |
| VAD 留在主进程 | 每 400ms 高频调用 + 模型极小，跨进程不划算（唯一例外） |
| TTS 音频走 job long-poll | 保留流式起播延迟，消除 PUB/SUB 首包竞态，并提供可靠取消与背压 |
| 主进程使用 LocalRuntimeBus | 内部事件/服务不依赖 protobuf、ZeroMQ 或 BusHub；高频 payload 可用原生对象 |
| RemoteBusNode 只用于边界 | 模型调用、健康探活和兼容出口保留现有 wire 契约；内部主链路不受其故障影响 |
| BusCompatibilityBridge 独立排队 | 保留 recorder/debug/旧服务兼容，同时不让外部慢消费者反压实时链路 |
| Supervisor 只管理进程，ModelManager 管理模型 | 避免 OS 进程重启策略与模型 load/unload 状态机互相覆盖 |
| readiness 与 `active_calls` 分离 | `ready` 不代表空闲；drain/unload 可拒绝新调用并安全等待在途推理 |
| 模型管理采用异步 operation | load/unload 可能耗时，control lane 只校验/入队/查询，健康探活不被阻塞 |
| 兼容 `models/*` 作为同步 wrapper | 保留旧服务名与 payload；新客户端使用 submit/status/cancel 获得幂等与可观测状态 |
| 显存采用策略化准入和驱逐 | 只驱逐低优先级、可驱逐、未固定且无 active lease 的模型；local_chat/STT 默认保留 |
| `worker_fatal` 仅表示运行时不可恢复 | 普通模型失败/OOM 先隔离、驱逐和有限重试，避免把局部故障升级成进程重启风暴 |
| yuki 是显式 bus_host | supervisor 能区分总线故障与 worker 故障，主进程重启不连带重启 worker |
