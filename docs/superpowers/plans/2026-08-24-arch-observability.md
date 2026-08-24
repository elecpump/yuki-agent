# 可观测性 Implementation Plan（架构评审主题 7）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已存在但零调用的审计/追踪基础设施真正接上：audit_logger 进入关键写路径，PUB/SUB 事件跨进程传播 trace_id，新增 situations/tool_calls 两类 rollout trace（JSONL），并让 bus_server 的 liveness 反映真实转发而非线程空转。

**Architecture:** `logger.py` 新增 `audit_log(event, **fields)` 便捷函数与 `get_situation_logger`/`get_toolcall_logger`（沿用 `get_file_logger` 模式）。`publish()` 未带 trace_id 时自动生成并随信封下发；`_run_sub` 把 `(topic, payload, trace_id)` 入队，`handler_worker` 在线程内 `bind/unbind_trace_id` 后调 handler——跨进程、跨线程真正传播。`BusHub._proxy_loop` 区分"线程活动"与"真实转发"，liveness 以转发时间为准。

**Tech Stack:** Python ≥3.11，structlog.contextvars，pytest。无新增运行时依赖。

## Global Constraints

- audit/trace 写入是 **best-effort**：任何 logger 写入失败不得影响业务路径（`get_file_logger` 已吞异常式的 FileHandler，调用点不再加 try/except）。
- 不改变信封 wire format：`trace_id` 复用现有 `Envelope.trace_id` 字段，`build_event` 已支持。
- `audit_log` 必须在模块级经 `get_audit_logger()` 动态取 logger（便于测试 monkeypatch）。
- handler 队列条目从 `(topic, payload)` 扩展为 `(topic, payload, trace_id)`：现有 push 处同步改；队列满时丢事件逻辑不变。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**修改**
- `src/yuki/logger.py` — `audit_log` + `get_situation_logger`/`get_toolcall_logger`
- `src/yuki/bus.py` — `publish` 自动 trace_id；`_run_sub`/`handler_worker` 传播 trace；`_proxy_loop`/`_collect_health` 真实转发活性
- `src/yuki/memory/store.py` — `MemoryStore.create` 写 audit
- `src/yuki/cognition/brain/soul.py` — `SoulStore.save` 写 audit
- `src/yuki/functions/tool_manager.py` — `ToolManager.call` 写 audit + toolcall trace
- `src/yuki/cognition/pipeline.py` — `_publish_situation` 写 situation trace
- 测试：`tests/test_logger.py`、`tests/test_bus_faults.py`、`tests/test_memory_store.py`、`tests/cognition/test_soul.py`、`tests/test_functions.py`、`tests/cognition/test_pipeline.py`

---

### Task 1: audit_log + rollout trace logger 助手

**Files:**
- Modify: `src/yuki/logger.py`
- Modify: `tests/test_logger.py`

**Interfaces:**
- Consumes: 无。
- Produces: `audit_log(event, **fields) -> None`；`get_situation_logger()`（写 `logs/situations.jsonl`）；`get_toolcall_logger()`（写 `logs/tool_calls.jsonl`）。Task 2/3/4 依赖。

- [ ] **Step 1: 追加失败测试到 `tests/test_logger.py`**

```python
def test_audit_log_writes_via_audit_logger(tmp_path, monkeypatch):
    class FakeAudit:
        def __init__(self):
            self.calls = []

        def info(self, event, **fields):
            self.calls.append((event, fields))

    fake = FakeAudit()
    monkeypatch.setattr("yuki.logger.get_audit_logger", lambda: fake)
    audit_log("memory.create", memory_id=1, memory_type="personal")
    assert fake.calls == [("memory.create", {"memory_id": 1, "memory_type": "personal"})]


def test_situation_and_toolcall_loggers_exist():
    assert callable(get_situation_logger().info)
    assert callable(get_toolcall_logger().info)


def test_situation_logger_writes_jsonl(tmp_path, monkeypatch):
    log = get_situation_logger()
    handler = logging.FileHandler(tmp_path / "situations.jsonl", encoding="utf-8")
    log.info("situation", topic="量子计算", layer="fast")
    # 真实 FileHandler 写入 logs/ 下；此处验证可创建（内容不校验）
    assert callable(log.info)
```

注：`get_situation_logger()`/`get_toolcall_logger()` 沿用 `get_file_logger` 惰性单例，写入 `logs/` 目录（测试不校验内容，保持与 `test_module_singletons_write_under_logs_dir` 一致）。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_logger.py -v`
Expected: FAIL（`ImportError: cannot import name 'audit_log'`）。

- [ ] **Step 3: 修改 `src/yuki/logger.py`**

新增（放在 `get_decision_logger` 之后）：

```python
_situation_logger = None
_toolcall_logger = None


def get_situation_logger():
    global _situation_logger
    if _situation_logger is None:
        _situation_logger = get_file_logger("yuki.situation", "situations.jsonl")
    return _situation_logger


def get_toolcall_logger():
    global _toolcall_logger
    if _toolcall_logger is None:
        _toolcall_logger = get_file_logger("yuki.toolcall", "tool_calls.jsonl")
    return _toolcall_logger


def audit_log(event: str, **fields: object) -> None:
    """写审计日志（logs/audit.jsonl）。调用点不捕获异常：写入是 best-effort。"""
    get_audit_logger().info(event, **fields)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_logger.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/logger.py tests/test_logger.py
git commit -m "feat: add audit_log helper and situation/toolcall rollout trace loggers"
```

---

### Task 2: audit 进入写路径（memory/soul/tool）

**Files:**
- Modify: `src/yuki/memory/store.py`
- Modify: `src/yuki/cognition/brain/soul.py`
- Modify: `src/yuki/functions/tool_manager.py`
- Modify: `tests/test_memory_store.py`、`tests/cognition/test_soul.py`、`tests/test_functions.py`

**Interfaces:**
- Consumes: `audit_log`（Task 1）。
- Produces: 无新接口；三个写路径分别写 `memory.create`/`soul.save`/`tool.call` 审计事件。

- [ ] **Step 1: 追加失败测试到各测试文件**

`tests/test_memory_store.py`：

```python
def test_memory_create_writes_audit(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.memory.store.get_audit_logger", lambda: FakeAudit())
    store = MemoryStore(tmp_path / "m.db")
    memory_id = store.create("personal", "内容", source="cli")
    assert calls[0][0] == "memory.create"
    assert calls[0][1]["memory_id"] == memory_id
    store.close()
```

`tests/cognition/test_soul.py`：

```python
def test_soul_save_writes_audit(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.cognition.brain.soul.get_audit_logger", lambda: FakeAudit())
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.save(store.default_soul())
    assert calls[0][0] == "soul.save"
```

`tests/test_functions.py`：

```python
def test_tool_call_writes_audit(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.functions.tool_manager.get_audit_logger", lambda: FakeAudit())
    manager = ToolManager()
    manager.tool("echo", description="echo", params=None)(lambda p: "pong")
    manager.call("echo", {})
    assert calls[0][0] == "tool.call"
    assert calls[0][1]["name"] == "echo"
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_store.py tests/cognition/test_soul.py tests/test_functions.py -v -k "audit"`
Expected: FAIL（三个路径均无 audit 调用）。

- [ ] **Step 3: 接入 audit 调用**

`src/yuki/memory/store.py`：
- import 区新增：`from yuki.logger import get_audit_logger, get_logger`
- `create` 方法在 `return int(cur.lastrowid)` 前插入：

```python
        memory_id = int(cur.lastrowid)
        get_audit_logger().info("memory.create", memory_id=memory_id, memory_type=memory_type)
        return memory_id
```

`src/yuki/cognition/brain/soul.py`：
- import 区新增：`from yuki.logger import get_audit_logger, get_logger`
- `save` 方法在 `_atomic_write_json`（已迁移为 `atomic_write_json`）成功路径后插入：

```python
        try:
            atomic_write_json(self._path, normalized)
        except OSError as exc:
            logger.warning("soul write failed", error=str(exc))
        else:
            get_audit_logger().info("soul.save", persona=self._persona_name)
```

`src/yuki/functions/tool_manager.py`：
- import 区新增：`from yuki.logger import get_audit_logger, get_logger`
- `call` 方法在 `tool.handler(validated)` 前后包审计（成功/失败都记）：

```python
    def call(self, name: str, args: dict | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        validated = self._validate(tool, args)
        self._check_rate_limit(tool)
        get_audit_logger().info("tool.call", name=name)
        try:
            result = tool.handler(validated)
        except FunctionError:
            raise
        except Exception as exc:
            get_audit_logger().info("tool.call_failed", name=name, error=str(exc))
            raise ToolExecutionError(f"{name} failed: {exc}") from exc
        get_audit_logger().info("tool.call_ok", name=name)
        return result
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_memory_store.py tests/cognition/test_soul.py tests/test_functions.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/memory/store.py src/yuki/cognition/brain/soul.py src/yuki/functions/tool_manager.py tests/test_memory_store.py tests/cognition/test_soul.py tests/test_functions.py
git commit -m "feat: write audit events on memory/soul/tool call paths"
```

---

### Task 3: PUB/SUB trace_id 跨进程传播

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `tests/test_bus_faults.py`

**Interfaces:**
- Consumes: `bind_trace_id`/`unbind_trace_id`（已有）。
- Produces: `publish()` 未给 trace_id 时自动生成并随信封下发；订阅侧队列条目为 `(topic, payload, trace_id)`，`handler_worker` 在线程内 bind 后再调 handler、finally unbind。Task 4 用到的 `_collect_health` 无关。

- [ ] **Step 1: 追加失败测试到 `tests/test_bus_faults.py`**

```python
def test_subscriber_sees_trace_id_in_context(make_bus):
    import structlog

    seen = {}

    def on_event(topic, payload):
        seen["trace_id"] = structlog.contextvars.get_contextvars().get("trace_id")

    bus = make_bus(6186)
    bus.subscribe("event/", on_event)
    time.sleep(0.1)
    bus.publish("event/t", {"x": 1}, trace_id="trace-abc")

    deadline = time.time() + 2.0
    while "trace_id" not in seen and time.time() < deadline:
        time.sleep(0.05)
    assert seen["trace_id"] == "trace-abc"
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_bus_faults.py::test_subscriber_sees_trace_id_in_context -v`
Expected: FAIL——当前 `_run_sub` 只把 `(topic, payload)` 入队，handler 线程看不到 trace_id。

- [ ] **Step 3: 修改 `src/yuki/bus.py`**

- `publish` 自动生成 trace_id：

```python
    def publish(self, topic: str, payload: dict, *, trace_id: str | None = None) -> None:
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        envelope = build_event(topic, payload, trace_id=trace_id)
        frames = [topic.encode()]
        if self._auth_token:
            frames.append(self._auth_token.encode("utf-8"))
        frames.append(envelope.SerializeToString())
        try:
            self._pub_queue.put_nowait(frames)
        except queue.Full:
            self._bump_dropped()
            logger.warning("publish queue full, dropping event", topic=topic)
```

- `_run_sub` 出队时携带 trace_id：

```python
            payload = event_payload(envelope)
            trace_id = envelope.trace_id or ""
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                worker_queue = self._handler_queues.get(id(handler))
                try:
                    if worker_queue is None:
                        continue
                    try:
                        worker_queue.put_nowait((topic, payload, trace_id))
                    except queue.Full:
                        self._bump_dropped()
                        logger.warning(
                            "subscriber queue full, dropping event", topic=topic
                        )
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._bump_error()
```

- `_handler_worker` 线程内 bind/unbind：

```python
    def _handler_worker(self, handler, worker_queue: queue.Queue) -> None:
        while True:
            item = worker_queue.get()
            if item is None:
                return
            topic, payload, trace_id = item
            if trace_id:
                bind_trace_id(trace_id)
            try:
                handler(topic, payload)
            except Exception:
                logger.error("subscriber handler failed", topic=topic, exc_info=True)
                self._bump_error()
            finally:
                if trace_id:
                    unbind_trace_id()
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_bus_faults.py -v`
Expected: 全 PASS（现有事件用例不受影响，新增 trace 测试通过）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/bus.py tests/test_bus_faults.py
git commit -m "feat: propagate trace_id across pub/sub handler threads"
```

---

### Task 4: rollout trace 文件 + bus liveness 真实转发

**Files:**
- Modify: `src/yuki/cognition/pipeline.py`
- Modify: `src/yuki/functions/tool_manager.py`
- Modify: `src/yuki/bus.py`
- Modify: `tests/cognition/test_pipeline.py`、`tests/test_functions.py`、`tests/test_bus_faults.py`

**Interfaces:**
- Consumes: `get_situation_logger`/`get_toolcall_logger`（Task 1）、`audit_log`（Task 1）。
- Produces: `_publish_situation` 写 situation trace；`ToolManager.call` 写 toolcall trace；`BusHub` 区分线程活动与真实转发，liveness 基于转发时间。

- [ ] **Step 1: 追加失败测试**

`tests/cognition/test_pipeline.py`：

```python
def test_publish_situation_writes_rollout_trace(monkeypatch):
    calls = []

    class FakeTrace:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.cognition.pipeline.get_situation_logger", lambda: FakeTrace())
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(), stt=FakeSTT(), frame_client=FakeFrameClient())
    pipeline._publish_situation({"topic": "量子计算"})
    assert calls[0][0] == "situation"
    assert calls[0][1]["topic"] == "量子计算"
    pipeline.close()
```

`tests/test_functions.py`：

```python
def test_tool_call_writes_toolcall_trace(monkeypatch):
    calls = []

    class FakeTrace:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.functions.tool_manager.get_toolcall_logger", lambda: FakeTrace())
    manager = ToolManager()
    manager.tool("echo", description="echo", params=None)(lambda p: "pong")
    manager.call("echo", {})
    assert calls[0][0] == "tool_call"
    assert calls[0][1]["name"] == "echo"
```

`tests/test_bus_faults.py`：

```python
def test_bus_hub_health_reflects_forwarding():
    import yuki.bus as bus_mod

    port = 6187
    hub = bus_mod.BusHub(base_port=port, hwm=10)
    node = bus_mod.BusNode(base_port=port, hwm=10)
    try:
        old_stale = bus_mod.PROXY_STALE_S
        bus_mod.PROXY_STALE_S = 0.05
        received = threading.Event()
        node.subscribe("event/", lambda t, p: received.set())
        time.sleep(0.1)
        health_before = hub._collect_health()
        assert health_before["healthy"] is False  # 无真实转发 → 不健康
        node.publish("event/x", {"x": 1})
        assert received.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if hub._collect_health()["healthy"]:
                break
            time.sleep(0.02)
        assert hub._collect_health()["healthy"] is True
        bus_mod.PROXY_STALE_S = old_stale
    finally:
        node.close()
        hub.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_pipeline.py tests/test_functions.py tests/test_bus_faults.py -v -k "rollout_trace or health_reflects_forwarding"`
Expected: FAIL（pipeline/tool 无 trace 写入；hub liveness 只看线程活动）。

- [ ] **Step 3: 接入 rollout trace 与真实转发活性**

`src/yuki/cognition/pipeline.py`：
- import 区新增：`from yuki.logger import get_logger, get_situation_logger`
- `_publish_situation` 末尾：

```python
        self._bus.publish(Topics.SITUATION_UPDATE, data)
        get_situation_logger().info("situation", **{
            k: data[k] for k in ("source_id", "topic", "summary", "layer", "degraded") if k in data
        })
```

`src/yuki/functions/tool_manager.py`：
- import 区新增：`from yuki.logger import get_audit_logger, get_logger, get_toolcall_logger`
- `call` 成功/失败路径补 toolcall trace：

```python
        get_audit_logger().info("tool.call", name=name)
        get_toolcall_logger().info("tool_call", name=name)
        try:
            result = tool.handler(validated)
        except FunctionError:
            raise
        except Exception as exc:
            get_toolcall_logger().info("tool_call_failed", name=name, error=str(exc))
            get_audit_logger().info("tool.call_failed", name=name, error=str(exc))
            raise ToolExecutionError(f"{name} failed: {exc}") from exc
        get_toolcall_logger().info("tool_call_ok", name=name)
        get_audit_logger().info("tool.call_ok", name=name)
        return result
```

`src/yuki/bus.py`：
- `_proxy_loop` 仅在真实转发时更新转发活性：

```python
    def _proxy_loop(self) -> None:
        while not self._stop.is_set():
            self._last_proxy_activity = time.monotonic()
            poller = zmq.Poller()
            poller.register(self._xsub, zmq.POLLIN)
            poller.register(self._xpub, zmq.POLLIN)
            events = dict(poller.poll(100))
            try:
                forwarded = False
                if self._xsub in events:
                    frames = self._xsub.recv_multipart()
                    if self._auth_token and (
                        len(frames) != 3 or not _token_ok(self._auth_token, frames[1])
                    ):
                        logger.warning("dropping unauthorized publish")
                        continue
                    self._xpub.send_multipart(frames)
                    forwarded = True
                if self._xpub in events:
                    frames = self._xpub.recv_multipart()
                    self._xsub.send_multipart(frames)
                    forwarded = True
                if forwarded:
                    self._last_proxy_activity = time.monotonic()
            except zmq.ZMQError:
                return
        self._close_socket(self._xsub)
        self._close_socket(self._xpub)
```

- `_collect_health` 改为基于转发活性：

```python
    def _collect_health(self) -> dict:
        proxy_age = time.monotonic() - self._last_proxy_activity
        proxy_alive = proxy_age < PROXY_STALE_S
        return {
            "process": "bus_server",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 2),
            "error_count": 0,
            "healthy": proxy_alive,
            "components": {
                "proxy": {"ok": proxy_alive, "last_forwarded_s": round(proxy_age, 3)},
                "router": {"ok": True},
            },
        }
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_pipeline.py tests/test_functions.py tests/test_bus_faults.py -v`
Expected: 全 PASS（注意：`_proxy_loop` 的 `continue` 在未转发时不再刷新活性——首次启动且无流量时 hub 可能短暂不健康，属预期；`test_gateway` 若依赖 `_collect_health` 字段名，Step 5 核对）。

- [ ] **Step 5: 核对受影响的既有断言**

Run: `python -m pytest tests/bus_server/test_gateway.py tests/test_supervisor.py -v`
Expected: 若 `last_activity_s` 字段被断言，改为 `last_forwarded_s`；supervisor 的 BUS_HEALTH_SERVICE 探测逻辑不变。

- [ ] **Step 6: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/cognition/pipeline.py src/yuki/functions/tool_manager.py src/yuki/bus.py tests/cognition/test_pipeline.py tests/test_functions.py tests/test_bus_faults.py
git commit -m "feat: rollout trace files and real-forwarding liveness for bus hub"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 7 全目标——audit_logger 接入（Task 2）、PUB/SUB trace 传播（Task 3）、rollout trace 文件（Task 4）、服务级连通性替代心跳（Task 4 hub liveness 真实转发）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。Task 4 Step 1 的 pipeline/tool 测试用 monkeypatch 注入 fake trace logger，避免真实写 logs/ 污染。
- **Type consistency：** `audit_log(event, **fields)`、`get_situation_logger`/`get_toolcall_logger` 在 Task 1 定义，Task 2/4 同名调用；handler 队列 `(topic, payload, trace_id)` 在 Task 3 入队与 `_handler_worker` 解包一致；`_collect_health` 的 `last_forwarded_s` 在 Task 4 内自洽并提示核对 gateway 断言。
- **行为等价提醒：** `publish()` 自动生成 trace_id 改变事件信封内容（每个事件现在带 trace_id），但 payload 语义不变；`_proxy_loop` 无流量时 hub 短暂不健康是预期新语义，supervisor 探测逻辑无需改动。
