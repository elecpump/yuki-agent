# Yuki Agent 架构加固设计

> 日期：2026-08-13
> 状态：设计已批准，待实现
> 范围：架构评审 #1–#10 与 #12（#11 已核对证伪——其声称的 SDD spec 要求在实际 spec 文档中不存在，整体丢弃）

## 1. 背景与目标

架构评审指出现有实现存在进程职责混杂、全局可变状态、主题碎片化、健康检查缺失、载荷无契约、Config 扁平、关闭无序等问题。本设计一次性修复 #1–#10 与 #12，行为契约保持等价（现有 e2e/smoke 断言不变）。

关键决策（已确认）：

| 决策点 | 结论 |
|---|---|
| Bus 拆分 API | 彻底重命名 `BusHub` + `BusNode`，删除 `MessageBus`，不留别名 |
| SITUATION_UPDATE 消费 | L1Responder 订阅并存为 context，不自动开口 |
| 载荷契约 | TypedDict 静态约束，不改 codec |
| Config env | 嵌套 + 分段命名，不兼容旧扁平 env |
| 音频 base64 | 保留，文档标注（Struct 约束，留待 typed-message 迁移） |
| 稳定内容观察 | `FOCUS_CHANGED`/scroll 只作为 raw signal；认知层消费绑定 `frame_id` 的 `CONTENT_READY` |
| 情境证据链 | `SITUATION_UPDATE` 保留 `frame_id`/source/observation/cache provenance，Brain trace 记录轻量证据摘要 |

## 2. Bus 拆分（#1）

`src/yuki/bus.py` 重构为两个类，共享协议层：

- **`BusHub`**：仅持有 XSUB/XPUB/ROUTER，线程 `_proxy_loop`/`_router_loop`。API：`__init__(base_port, hwm)`、`close()`。
- **`BusNode`**：仅持有 PUB/DEALER + 惰性 SUB，线程 `_dealer_loop`/`_register_loop`/`_run_sub`。API：`__init__(base_port, hwm, register_interval)`、`publish(topic, payload)`、`subscribe(prefix, handler)`（多 handler + 前缀匹配）、`request(service, payload, timeout_ms=2000) -> dict`、`respond(service, handler)`、`error_count` 只读属性、`close()`。
- 共享：`BusError`、`BusTimeoutError`、`_matches`、`zmq.Context.instance()`。
- `MessageBus` 删除，`_error_count` 内部字段改为经 `error_count` 属性读取。

调用点迁移：

| 文件 | 新用法 |
|---|---|
| `bus_server/main.py` | `BusHub` |
| `cognition`/`interaction`/`perception`/`recorder`/`supervisor` | `BusNode` |

## 3. 进程骨架（#2/#3/#9/#10）

新增 `src/yuki/process.py`，提供 `ProcessAgent` 抽象基类：

```python
class ProcessAgent(ABC):
    name: str                                # "perception" 等
    def __init__(self, config)               # config / bus(BusNode) / shutdown / health
    def setup(self)                          # 子类钩子：装配并 start 组件
    def teardown(self)                       # 子类钩子：逆序 stop 组件
    def loop(self)                           # 默认：while not shutdown_requested: wait(1.0)
    def health_components(self) -> dict[str, Callable[[], HealthStatus]]:  # 返回 {}
    def run(self)                            # 信号→health.start→setup→loop→finally:
                                             #   health.stop→teardown→bus.close
```

具体 Agent：

- **`PerceptionAgent`**：持有 SensitiveDetector/ScrollIdleDetector/FrameStrategy/capture/monitor/audio/scroll_hook。`setup()` 组装并 start；`teardown()` 按 **scroll_hook→capture→monitor→audio** 逆序 stop。删除模块级 `_perception_state` 与 `build_perception`，构造参数可注入 fake 保留测试缝。
- **`CognitionAgent`**：持有 PerceptionPipeline + L1Responder，`setup()` 装配并订阅。删除 `build_cognition` 与手写装配。
- **`InteractionAgent`**：持有 HotkeyManager + 新增 **TTS / FocusManager / VolumeController 接口桩**（`speak(text)` 等空实现），`on_reply → tts.speak`；处理 `--trigger-after`。TTS 桩的 `speak(text)` 输出 `[yuki] {text}` 到 stdout（控制台降级，保持 e2e 断言 `[yuki] 我在，你说。` 等价）。
- **`BusServerAgent`**：覆写 `_make_bus()` 返回 `BusHub`。
- **`RecorderAgent`**：覆写 `loop()` 做帧抓取，复用信号/bus/健康框架。

各 `main.py` 收敛为 `Agent(config).run()` 数行。

**`ShutdownManager`（#9）**：新增 `register_cleanup(name, fn, priority=0)` 与 `run_cleanups()`（按 priority 逆序执行）。`ProcessAgent.run` 的 finally 中调用。组件停止顺序由各 Agent 的 `teardown()` 显式负责，不再依赖 finally 里手动遍历。

## 4. 健康检查（#5）

重写 `src/yuki/health.py`：

```python
@dataclass
class HealthStatus:
    ok: bool
    detail: dict = field(default_factory=dict)

class HealthReporter:
    def __init__(self, bus: BusNode, process: str, heartbeat_interval: float = 5.0)
    def register_component(name, check: Callable[[], HealthStatus])
    def collect(self) -> dict      # {process, pid, uptime_s, error_count, healthy, components}
    def start(self)                # 注册 health/{process} REQ/REP + 心跳线程
    def stop(self)
```

- 心跳：每 `heartbeat_interval` 发布 `Topics.HEARTBEAT`（`event/heartbeat`，interfaces.md §4 已列）。
- `error_count` 取自 `BusNode.error_count` 属性。
- 组件健康检查（各 Agent 的 `health_components()`）：
  - 感知：AudioCapture（stream 存活）、capture（on_frame 已挂）、SystemMonitor（线程存活）、ScrollHook（running）。
  - 认知：VLM（loaded/degraded）、STT、L1、pipeline（frame_client 可达）。
  - 交互：TTS 桩（ok）、HotkeyManager（ok）。

## 5. 主题与载荷契约（#4/#7）

`src/yuki/topics.py`：单一 `Topics` 类，按域分组，合并 `TopicsExt`：

```
event/ : AWAKE("event/awake") REPLY("event/reply") FOCUS_CHANGED("event/focus_changed")
         CONTENT_READY("event/perception/content_ready")
         SITUATION_UPDATE("event/perception/situation_update")
         USER_UTTERANCE("event/perception/user_utterance") HEARTBEAT("event/heartbeat")
audio/ : MIC("audio/mic") TTS_REF("audio/tts_ref")
```

删除 `cognition/topics_ext.py`，全仓引用迁移（pipeline、l1_responder、各测试）。

新增 `src/yuki/payloads.py`：TypedDict 定义载荷（`AwakePayload`、`ReplyPayload`、`FocusChangedPayload`、`ContentReadyPayload`、`SituationUpdatePayload`、`UserUtterancePayload`、`MicPayload`、`HeartbeatPayload`、`FrameResult`、`HealthResult`）。handler 签名与 `request()` 返回值用类型注解约束，零运行时开销。

## 6. Config 嵌套（#8）

```
Config(persona_name: str, bus: BusConfig, logging: LoggingConfig,
       supervisor: SupervisorConfig, health: HealthConfig)
```

- `BusConfig`: base_port/hwm；`LoggingConfig`: level；`SupervisorConfig`: 四个 restart 参数；`HealthConfig`: timeout_ms/heartbeat_interval_s。
- **移除 `bus_role` 字段**：进程类型由 Agent 类决定（BusServerAgent→BusHub，其余→BusNode），role 不再需要。
- env 分段命名：`YUKI_BUS_BASE_PORT`、`YUKI_BUS_HWM`、`YUKI_LOGGING_LEVEL`、`YUKI_SUPERVISOR_RESTART_BASE_DELAY`、`YUKI_SUPERVISOR_RESTART_MAX_DELAY`、`YUKI_SUPERVISOR_RESTART_WINDOW`、`YUKI_SUPERVISOR_RESTART_MAX_PER_WINDOW`、`YUKI_HEALTH_TIMEOUT_MS`、`YUKI_HEALTH_HEARTBEAT_INTERVAL_S`。旧扁平 env（`YUKI_BASE_PORT` 等）不再识别。
- `config.example.yaml` 重排为嵌套结构；根目录 `config.yaml` 自动发现保留。
- 全量迁移：`supervisor/main.py` 的 env 构建（去掉 `YUKI_BUS_ROLE`，改用 `YUKI_BUS_BASE_PORT`）与所有测试。

## 7. 数据流接入（#6）

`L1Responder` 新增订阅 `Topics.SITUATION_UPDATE`：收到后存 `_context`，不回复。`on_awake`/`on_user_utterance` 将情境传入 `l1.reply(text, context)`（L1Engine 已支持 context 参数，l1.py:23）。主动开口决策在 L1 留 TODO 注释，不改变现有行为。同时修正 PerceptionPipeline docstring 中对消费方的错误描述。

## 7.1 稳定内容观察与帧绑定（2026-08-16 增补）

新增 `StableContentObservation`，把 `FOCUS_CHANGED` 与 scroll idle 从原始输入信号提升为稳定内容事件。感知层先记录最近焦点与待处理原因，等 `make_frame_service` 确认新帧已存入后，再发布 `Topics.CONTENT_READY`。这样同页滚动、焦点切换、截图门控之间的时序由感知层集中处理，认知层只消费“有证据的内容快照”。

`make_frame_service` 内部新增 `FrameStore`，为每次存入的真实帧或敏感黑帧分配递增 `frame_id`，保留 bounded recent frames。`frame` REQ/REP 服务继续支持 `{}` 获取 latest，同时支持 `{"frame_id": int}` 获取指定帧；未命中返回 `{}` 并由调用方降级。`ContentReadyPayload` 携带 `frame_id`、`frame_ts`、尺寸和 `sensitive` 标记，`PerceptionPipeline` 优先用 `FrameClient.get_by_id(frame_id)` 读取对应帧。旧 `event/focus_changed` 保留给 recorder/backcompat；由感知层发出的 raw focus 会带 `content_ready_deferred`，pipeline 忽略该事件，避免重复理解或读到更新后的 latest。

测试覆盖：

- `tests/perception/test_observation.py`：focus/scroll 必须等待已识别帧后才发布 `CONTENT_READY`。
- `tests/perception/test_capture.py`：frame service 分配 `frame_id` 并支持按 ID 读取。
- `tests/cognition/test_frame_client.py`：`FrameClient.get_by_id()` 发送指定帧请求。
- `tests/cognition/test_pipeline.py`：`CONTENT_READY(frame_id=...)` 必须读取绑定帧，即使 latest 已变化。

## 7.2 情境证据链（2026-08-16 增补）

新增 `src/yuki/cognition/situation.py`，把 `CONTENT_READY`、指定 frame 和 VLM 结果组装为统一 `SituationUpdatePayload`。该 module 的 interface 负责 source 选择、`scroll_band` 归一化、VLM cache key、`situation_id` 与 provenance 字段填充；`PerceptionPipeline` 只负责读取 frame、调用 VLM 和发布结果。删除这个 module 会让相同的字段推导散落回 pipeline、Brain trace 与 context 测试中，因此它提供了 locality。

`SITUATION_UPDATE` 现在保留：

- `situation_id`：优先为 `frame:{frame_id}`，无 frame 时退化为 legacy 标识。
- 证据来源：`frame_id`、`frame_ts`、`frame_width`、`frame_height`、`source_id`、`source_app`、`source_title`。
- 观察信息：`observation_reason`、`observation_ts`、`scroll_band`、可选 `scroll_percent`。
- VLM 复用信息：`cache_key`、`degraded`、`reason`。

`WorkingContext.update_situation()` 原样保存该 payload，snapshot/restore 往返不裁掉 provenance。`DecisionTrace` 不记录完整 summary/key_points，改为 `situation_provenance` 摘要（`situation_id`、`frame_id`、`source_id`、`scroll_band`、`observation_reason`、`frame_ts`），便于审计和回放，同时避免 trace 复制大段内容。

测试覆盖：

- `tests/cognition/test_situation.py`：payload 构建、敏感降级和 `scroll_band` 归一化。
- `tests/cognition/test_pipeline.py`：pipeline 发布的 `SITUATION_UPDATE` 带 provenance。
- `tests/cognition/test_hub.py`：Decision trace 写入轻量 `situation_provenance`。
- `tests/cognition/context/test_working.py`：WorkingContext snapshot/restore 保留 provenance。

## 8. 代码质量修复（#12）

| 问题 | 修法 |
|---|---|
| system_monitor.py 顶层 `import win32gui`（非 Windows 崩溃） | 模块内 try/except 惰性导入，缺库时 graceful degrade |
| capture.py `latest` dict 多线程无锁 | 引入 `FrameStore`，用 `threading.Lock` 保护 latest 与按 `frame_id` 查询 |
| vlm.py `__import__("torch")` | 改为函数内普通 `import torch` |
| responder.py + `build_cognition` 死代码 | 删除文件/函数 + 相关测试 |
| logger.py 模块级创建 `logs/` 目录 | audit/decision logger 改惰性函数 `get_audit_logger()`/`get_decision_logger()` |
| 音频帧 base64 浪费 33% 带宽 | 保留，加注释标注 Struct 约束与未来 typed-message 迁移点 |
| FakeBus.subscribe 用 dict，语义与生产不一致 | 新增 `tests/fakes.py` 共享 FakeBus：多 handler + 前缀匹配，对齐 BusNode |

## 9. 测试策略

- 更新：`test_bus.py`/`test_bus_faults.py`/`test_health.py`/`test_shutdown.py`/`test_config.py`/`test_supervisor_main.py`/`test_e2e.py`/`test_topics.py`/cognition/perception/interaction/recorder 各测试（适配新类名、Topics 合并、嵌套 Config、删除死代码）。
- 新增：BusHub/BusNode 行为、HealthReporter 心跳与聚合、ProcessAgent 生命周期、组件 health_check、L1Responder context 接入、嵌套 Config env 解析。
- 行为契约保持等价：e2e/smoke 断言不变。

## 10. 风险与兼容

- 无协议变更：wire format（Envelope/frame 结构/REGISTER）保持不变，多进程互通不受影响。
- 无运行时行为变化：数据流只新增 context 注入，不改变回复行为。
- 破坏性变更集中在内部 API 与 env 命名，仓库 pre-1.0，接受一次性迁移。
- 删除 `bus_role` 后，`supervisor/main.py` 子进程 env 不再注入 role。
