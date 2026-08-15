# Yuki Agent 架构加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次性修复架构评审 #1–#10 与 #12：Bus 拆分、进程骨架、健康检查、主题/契约、Config 嵌套、数据流接入、代码质量，行为契约保持等价。

**Architecture:** 引入 `ProcessAgent` 基类统一四个进程的生命周期（信号/健康/关闭顺序），`MessageBus` 拆为 `BusHub`/`BusNode`，`Topics`/`TopicsExt` 合并为单类，payload 用 TypedDict 静态约束，`HealthReporter` 做组件级健康 + 心跳，Config 改嵌套结构。

**Tech Stack:** Python ≥3.11（TypedDict/NotRequired），pydantic v2，pyzmq，protobuf，pytest。无新增运行时依赖。

## Global Constraints

- 协议不变：wire format（Envelope/帧结构/REGISTER 控制帧）不得改动，多进程互通不受影响。
- `MessageBus` 彻底删除，不留别名；`TopicsExt` 删除。
- env 命名 `YUKI_<SECTION>_<FIELD>`，不兼容旧扁平 env。
- `Topics.HEARTBEAT = "event/heartbeat"`（interfaces.md §4 已列，标可选——本次实现之）。
- e2e 行为等价：stdout 必须仍出现 `[yuki] 我在，你说。`。
- 不新增运行时依赖；`grpcio-tools` 仅 dev。
- 每个任务结束跑：`python -m pytest <本次测试> -v`；全仓回归用 `python -m pytest`（e2e 默认跳过）。
- 设计文档：`docs/superpowers/specs/2026-08-13-yuki-architecture-fixes-design.md`。
- **不提交** `docs/superpowers/specs/2026-08-13-yuki-architecture-fixes-design.md` 与 `docs/superpowers/plans/2026-08-13-yuki-architecture-fixes.md`（用户要求）。各任务 commit 用具体文件列表 stage，不要用 `git add -A` 带入这两个文档。

---

## 文件结构

**新增**
- `src/yuki/payloads.py` — TypedDict 载荷定义（#7）
- `src/yuki/process.py` — `ProcessAgent` 抽象基类（#2）
- `src/yuki/perception/agent.py` — `PerceptionAgent`（#3）
- `src/yuki/cognition/agent.py` — `CognitionAgent`（#2/#6）
- `src/yuki/interaction/agent.py` — `InteractionAgent` + TTS/FocusManager/VolumeController 桩（#10）
- `src/yuki/bus_server/agent.py` — `BusServerAgent`
- `src/yuki/recorder/agent.py` — `RecorderAgent`
- `tests/fakes.py` + `tests/__init__.py` — 生产语义一致的共享 FakeBus（#12）
- `tests/test_process.py`

**修改**
- `src/yuki/bus.py`（拆分）、`topics.py`（合并）、`config.py`（嵌套）、`shutdown.py`（清理注册）、`health.py`（重写）、`logger.py`（惰性文件日志）
- 各层 `main.py` 收敛为薄壳；`supervisor/main.py`（env 命名）；`config.example.yaml`（嵌套）
- `cognition/pipeline.py`、`cognition/l1_responder.py`（Topics 引用 + context 接入）、`cognition/vlm.py`（import torch）
- `perception/system_monitor.py`（惰性 win32）、`perception/capture.py`（锁）、`perception/audio.py`（注释）
- 测试：`test_bus.py`/`test_bus_faults.py`/`test_health.py`/`test_shutdown.py`/`test_config.py`/`test_topics.py`/`test_e2e.py`/`test_supervisor_main.py` + cognition/interaction/recorder/perception 各测试

**删除**
- `src/yuki/cognition/topics_ext.py`、`src/yuki/cognition/responder.py`、`tests/test_responder.py`、`tests/cognition/test_topics_ext.py`

---

### Task 1: 主题合并 + 载荷 TypedDict（#4/#7）

**Files:**
- Modify: `src/yuki/topics.py`
- Create: `src/yuki/payloads.py`
- Delete: `src/yuki/cognition/topics_ext.py`
- Modify: `src/yuki/cognition/pipeline.py`（Topics 引用 + docstring）
- Modify: `src/yuki/cognition/l1_responder.py`（Topics 引用）
- Test: `tests/test_topics.py`、`tests/cognition/test_topics_ext.py`（删除）、`tests/cognition/test_pipeline.py`、`tests/cognition/test_l1_responder.py`

**Interfaces:**
- Consumes: 无（纯新增/改名）。
- Produces: `Topics.SITUATION_UPDATE`/`USER_UTTERANCE`/`HEARTBEAT` 常量；`payloads.py` 导出 `AwakePayload`、`ReplyPayload`、`FocusChangedPayload`、`SituationUpdatePayload`、`UserUtterancePayload`、`MicPayload`、`HeartbeatPayload`、`FrameResult`、`HealthResult`。Task 4/7 依赖这些名字。

- [ ] **Step 1: 重写 `src/yuki/topics.py`**

```python
class Topics:
    AWAKE = "event/awake"
    REPLY = "event/reply"
    FOCUS_CHANGED = "event/focus_changed"
    SITUATION_UPDATE = "event/perception/situation_update"
    USER_UTTERANCE = "event/perception/user_utterance"
    HEARTBEAT = "event/heartbeat"
    MIC = "audio/mic"
    TTS_REF = "audio/tts_ref"
```

- [ ] **Step 2: 更新 `tests/test_topics.py`**

```python
from yuki.topics import Topics


def test_topic_constants():
    assert Topics.AWAKE == "event/awake"
    assert Topics.REPLY == "event/reply"
    assert Topics.FOCUS_CHANGED == "event/focus_changed"
    assert Topics.SITUATION_UPDATE == "event/perception/situation_update"
    assert Topics.USER_UTTERANCE == "event/perception/user_utterance"
    assert Topics.HEARTBEAT == "event/heartbeat"
    assert Topics.MIC == "audio/mic"
    assert Topics.TTS_REF == "audio/tts_ref"
```

- [ ] **Step 3: 删除 `tests/cognition/test_topics_ext.py` 与 `src/yuki/cognition/topics_ext.py`**

Run: `Remove-Item tests/cognition/test_topics_ext.py, src/yuki/cognition/topics_ext.py`

- [ ] **Step 4: 创建 `src/yuki/payloads.py`**

```python
from typing import NotRequired, TypedDict


class AwakePayload(TypedDict):
    source: str
    ts: float
    confidence: NotRequired[float]


class ReplyPayload(TypedDict):
    text: str
    ts: float


class FocusChangedPayload(TypedDict):
    app: str
    url: str
    title: str


class SituationUpdatePayload(TypedDict):
    source_id: str
    scroll_band: str
    topic: str
    summary: str
    content_type: str
    key_points: list[str]
    sensitive: bool
    degraded: bool
    reason: str
    ts: float


class UserUtterancePayload(TypedDict):
    text: str
    duration_s: float
    ts: float


class MicPayload(TypedDict):
    pcm: str
    sample_rate: int
    ts: float


class HeartbeatPayload(TypedDict):
    process: str
    ts: float
    healthy: bool
    components: dict[str, dict]


class FrameResult(TypedDict):
    png: str
    width: int
    height: int
    ts: float
    sensitive: bool


class HealthResult(TypedDict):
    process: str
    pid: int
    uptime_s: float
    error_count: int
    healthy: bool
    components: dict[str, dict]
```

- [ ] **Step 5: 迁移 pipeline.py 引用**

在 `src/yuki/cognition/pipeline.py`：
- 删除 `from yuki.cognition.topics_ext import TopicsExt`，改用 `from yuki.topics import Topics`（已导入）。
- 把 `TopicsExt.USER_UTTERANCE` 替换为 `Topics.USER_UTTERANCE`，`TopicsExt.SITUATION_UPDATE` 替换为 `Topics.SITUATION_UPDATE`。
- 修正类 docstring：`供 L1Responder（当前）/ ContextAssembler（未来 Brain）消费` → `当前由 L1Responder 作为 context 消费，不触发自动回复；未来 Brain 阶段接入主动评论。`

- [ ] **Step 6: 迁移 l1_responder.py 引用**

在 `src/yuki/cognition/l1_responder.py`：删除 `from yuki.cognition.topics_ext import TopicsExt`，把 `TopicsExt.USER_UTTERANCE` 替换为 `Topics.USER_UTTERANCE`（`Topics` 已导入）。

- [ ] **Step 7: 更新引用 TopicsExt 的测试**

- `tests/cognition/test_pipeline.py`：`from yuki.cognition.topics_ext import TopicsExt` → 改从 `yuki.topics` 导入（或直接用 `Topics`）；替换 `TopicsExt.SITUATION_UPDATE`/`USER_UTTERANCE` 为 `Topics.*`。
- `tests/cognition/test_l1_responder.py`：删除 `TopicsExt` 导入，`TopicsExt.USER_UTTERANCE` → `Topics.USER_UTTERANCE`。

- [ ] **Step 8: 运行主题/载荷相关测试**

Run: `python -m pytest tests/test_topics.py tests/cognition/test_pipeline.py tests/cognition/test_l1_responder.py -v`
Expected: 全 PASS。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: merge topics into single registry, add typed payloads"
```

---

### Task 2: Bus 拆分 BusHub/BusNode（#1）

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `src/yuki/bus_server/main.py`、`src/yuki/supervisor/main.py`、`src/yuki/recorder/cli.py`、`src/yuki/cognition/main.py`、`src/yuki/interaction/main.py`、`src/yuki/perception/main.py`（仅换类名，Task 6 再收敛结构）
- Test: `tests/test_bus.py`、`tests/test_bus_faults.py`、`tests/test_health.py`

**Interfaces:**
- Consumes: 无。
- Produces: `BusHub(base_port, hwm=1000)`，`BusNode(base_port, hwm=1000, register_interval=10.0)`；`BusNode` 方法 `publish/subscribe/request/respond`、属性 `error_count`；共享 `BusError`/`BusTimeoutError`。Task 4/6 依赖 `BusNode.error_count`。

- [ ] **Step 1: 重写 `tests/test_bus.py`（先红）**

```python
import threading
import time

from yuki.bus import BusHub, BusNode


def _hub_node(port):
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10)
    return hub, node


def _wait_sub(t=0.1):
    time.sleep(t)


def test_publish_subscribe_roundtrip():
    hub, node = _hub_node(6001)
    received = threading.Event()
    got = {}

    def on_event(topic, payload):
        got["topic"] = topic
        got["payload"] = payload
        received.set()

    node.subscribe("event/", on_event)
    _wait_sub()
    node.publish("event/awake", {"source": "hotkey"})
    assert received.wait(timeout=2.0)
    assert got["topic"] == "event/awake"
    assert got["payload"] == {"source": "hotkey"}
    hub.close()
    node.close()


def test_subscribe_filters_by_prefix():
    hub, node = _hub_node(6002)
    hits = []

    def on_awake(topic, payload):
        hits.append(payload)

    node.subscribe("event/awake", on_awake)
    _wait_sub()
    node.publish("event/reply", {"text": "hi"})
    node.publish("event/awake", {"source": "hotkey"})
    deadline = time.time() + 2.0
    while not hits and time.time() < deadline:
        time.sleep(0.05)
    assert hits == [{"source": "hotkey"}]
    hub.close()
    node.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_bus.py -v`
Expected: FAIL（`ImportError: cannot import name 'BusHub'`）。

- [ ] **Step 3: 重写 `src/yuki/bus.py`**

```python
import logging
import threading
import uuid
from typing import Callable

import zmq
from google.protobuf.message import DecodeError

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id
from yuki.proto.codec import (
    build_event,
    build_request,
    build_response_error,
    build_response_result,
    event_payload,
    parse_envelope,
    request_payload,
    response_result,
)

logger = get_logger("yuki.bus")


class BusError(Exception):
    pass


class BusTimeoutError(BusError):
    pass


def _matches(prefix: str, topic: str) -> bool:
    return topic.startswith(prefix)


class _Base:
    """共享生命周期：线程跟踪、socket 关闭、libzmq 4.3.5 signaler 规避。"""

    def __init__(self) -> None:
        self._ctx = zmq.Context.instance()
        self._closed = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _spawn(self, target) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._close_socket(getattr(self, "_xsub", None))
        self._close_socket(getattr(self, "_xpub", None))
        self._close_socket(getattr(self, "_router", None))
        self._close_socket(getattr(self, "_pub", None))
        self._close_socket(getattr(self, "_dealer", None))
        self._close_socket(getattr(self, "_sub", None))

    def _close_socket(self, sock) -> None:
        if sock is not None:
            try:
                sock.close(linger=0)
            except zmq.ZMQError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class BusHub(_Base):
    """枢纽：XSUB/XPUB/ROUTER，只做转发与 REQ/REP 路由。"""

    def __init__(self, base_port: int = 5555, hwm: int = 1000) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._service_map: dict[str, bytes] = {}
        self._xsub = self._ctx.socket(zmq.XSUB)
        self._xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        self._xpub = self._ctx.socket(zmq.XPUB)
        self._xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.RCVHWM, hwm)
        self._router.bind(f"tcp://127.0.0.1:{self._router_port}")
        self._spawn(self._proxy_loop)
        self._spawn(self._router_loop)

    def _proxy_loop(self) -> None:
        while not self._stop.is_set():
            poller = zmq.Poller()
            poller.register(self._xsub, zmq.POLLIN)
            poller.register(self._xpub, zmq.POLLIN)
            events = dict(poller.poll(100))
            try:
                if self._xsub in events:
                    frames = self._xsub.recv_multipart()
                    self._xpub.send_multipart(frames)
                if self._xpub in events:
                    frames = self._xpub.recv_multipart()
                    self._xsub.send_multipart(frames)
            except zmq.ZMQError:
                return
        self._close_socket(self._xsub)
        self._close_socket(self._xpub)

    def _router_loop(self) -> None:
        while not self._stop.is_set():
            if not self._router.poll(100):
                continue
            try:
                frames = self._router.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) < 3:
                continue
            sender = frames[0]
            if frames[1] == b"REGISTER":
                self._service_map[frames[2].decode()] = sender
                continue
            f1, f2 = frames[1], frames[2]
            try:
                envelope = parse_envelope(f2)
            except DecodeError:
                logger.warning("dropping malformed envelope from %s", sender)
                continue
            kind = envelope.WhichOneof("body")
            if kind == "request":
                provider = self._service_map.get(envelope.request.service, "")
                if not provider:
                    err = build_response_error(envelope.request.request_id, "service not found")
                    self._router.send_multipart([sender, err.SerializeToString()])
                else:
                    self._router.send_multipart([provider, sender, f2])
            elif kind == "response":
                self._router.send_multipart([f1, f2])
        self._close_socket(self._router)


class BusNode(_Base):
    """节点：PUB/DEALER/SUB，publish/subscribe/request/respond。"""

    def __init__(
        self,
        base_port: int = 5555,
        hwm: int = 1000,
        register_interval: float = 10.0,
    ) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._hwm = hwm
        self._register_interval = register_interval
        self._handlers: dict[str, list[Callable[[str, dict], None]]] = {}
        self._services: dict[str, Callable[[dict], dict]] = {}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._error_count = 0

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, hwm)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.SNDHWM, hwm)
        self._dealer.setsockopt(zmq.RCVHWM, hwm)
        self._dealer.connect(f"tcp://127.0.0.1:{self._router_port}")
        self._spawn(self._dealer_loop)
        self._spawn(self._register_loop)

    @property
    def error_count(self) -> int:
        return self._error_count

    def publish(self, topic: str, payload: dict) -> None:
        envelope = build_event(topic, payload)
        self._pub.send_multipart([topic.encode(), envelope.SerializeToString()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            handlers = self._handlers.setdefault(topic_prefix, [])
            handlers.append(handler)
        if not hasattr(self, "_sub"):
            self._sub = self._ctx.socket(zmq.SUB)
            self._sub.setsockopt(zmq.RCVHWM, self._hwm)
            self._sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")
            self._spawn(self._run_sub)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        trace_id = uuid.uuid4().hex
        envelope = build_request(service, rid, trace_id, payload)
        with self._lock:
            self._pending[rid] = {"event": event, "result": None}
        self._dealer.send_multipart([service.encode(), envelope.SerializeToString()])
        if not event.wait(timeout_ms / 1000.0):
            with self._lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._lock:
            resp = self._pending.pop(rid)["result"]
        if resp.response.HasField("error"):
            raise BusError(resp.response.error)
        return response_result(resp)

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        self._services[service] = handler
        self._dealer.send_multipart([b"REGISTER", service.encode()])

    def _dealer_loop(self) -> None:
        while not self._stop.is_set():
            if not self._dealer.poll(100):
                continue
            try:
                frames = self._dealer.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    envelope = parse_envelope(raw)
                except DecodeError:
                    continue
                if envelope.trace_id:
                    bind_trace_id(envelope.trace_id)
                if envelope.WhichOneof("body") != "request":
                    if envelope.trace_id:
                        unbind_trace_id()
                    continue
                handler = self._services.get(envelope.request.service)
                if handler is None:
                    reply = build_response_error(envelope.request.request_id, "service not found")
                else:
                    try:
                        result = handler(request_payload(envelope))
                        reply = build_response_result(envelope.request.request_id, result)
                    except Exception:
                        logger.error("responder handler failed", service=envelope.request.service)
                        self._error_count += 1
                        reply = build_response_error(envelope.request.request_id, "handler error")
                self._dealer.send_multipart([client_id, reply.SerializeToString()])
                if envelope.trace_id:
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    envelope = parse_envelope(frames[0])
                except DecodeError:
                    continue
                if envelope.WhichOneof("body") != "response":
                    continue
                rid = envelope.response.request_id
                with self._lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = envelope
                    entry["event"].set()
        self._close_socket(self._dealer)

    def _register_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=self._register_interval)
            services = list(self._services.keys())
            if not services:
                continue
            try:
                for service in services:
                    self._dealer.send_multipart([b"REGISTER", service.encode()])
            except zmq.ZMQError:
                return

    def _run_sub(self) -> None:
        while not self._stop.is_set():
            if not self._sub.poll(100):
                continue
            try:
                raw_topic, raw_payload = self._sub.recv_multipart()
            except zmq.ZMQError:
                return
            topic = raw_topic.decode()
            try:
                envelope = parse_envelope(raw_payload)
            except DecodeError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            if envelope.WhichOneof("body") != "event":
                logger.warning("dropping non-event envelope", topic=topic)
                continue
            payload = event_payload(envelope)
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                try:
                    handler(topic, payload)
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._error_count += 1
        self._close_socket(getattr(self, "_sub", None))
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_bus.py -v`
Expected: PASS。

- [ ] **Step 5: 更新 `tests/test_bus_faults.py`**

- `from yuki.bus import BusError, BusTimeoutError, MessageBus` → `from yuki.bus import BusError, BusTimeoutError, BusHub, BusNode`。
- fixture `make_bus`：改为创建 hub+node 对：

```python
@pytest.fixture()
def make_bus():
    buses = []

    def _make(port, **kwargs):
        hub = BusHub(base_port=port, hwm=10)
        node = BusNode(base_port=port, hwm=10, **kwargs)
        buses.extend([hub, node])
        return node

    yield _make
    for bus in buses:
        bus.close()
```

- `test_services_reregister_after_hub_restart`：`hub = make_bus(port, role="hub")` → `hub = BusHub(base_port=port, hwm=10)`；`node = make_bus(port, role="node", register_interval=0.2)` → `node = make_bus(port, register_interval=0.2)`；`hub2 = make_bus(port, role="hub")` → `hub2 = BusHub(base_port=port, hwm=10)`，并在 finally 关闭 hub/hub2。
- `test_bus_wire_format_is_protobuf`：改为两个 node 各连一个 hub：

```python
def test_bus_wire_format_is_protobuf(make_bus):
    hub = BusHub(base_port=6900, hwm=10)
    node = make_bus(6900)
    node.respond("ping", lambda p: {"echo": p["msg"]})
    time.sleep(0.2)

    captured = {}

    def on_awake(topic, payload):
        captured["payload"] = payload

    node.subscribe("event/awake", on_awake)
    time.sleep(0.2)
    node.publish("event/awake", {"source": "hotkey"})
    deadline = time.time() + 2.0
    while "payload" not in captured and time.time() < deadline:
        time.sleep(0.05)
    assert captured.get("payload") == {"source": "hotkey"}
    hub.close()
```

- `test_wire_frame_second_part_is_envelope`：`node = make_bus(6901, role="node")` → `node = make_bus(6901)`。
- `test_many_bus_create_close_cycles`：循环内改 `hub = BusHub(base_port=6800 + i, hwm=10); node = BusNode(base_port=6800 + i, hwm=10)`，`node.respond(...)`，`node.request(...)`，最后 `hub.close(); node.close()`。

- [ ] **Step 6: 更新 `tests/test_health.py` fixture（本任务只改类名，Step 实现见 Task 4）**

`from yuki.bus import MessageBus` → `from yuki.bus import BusHub, BusNode`；fixture `_make(port, role="hub")` 改为创建 hub + node 并返回 node：

```python
@pytest.fixture()
def make_bus():
    buses = []

    def _make(port, **kwargs):
        hub = BusHub(base_port=port, hwm=10)
        node = BusNode(base_port=port, hwm=10, **kwargs)
        buses.extend([hub, node])
        return node

    yield _make
    for bus in buses:
        bus.close()
```

注意：`test_health.py` 的 `register_health_service` 在 Task 4 会被替换，本任务先只改导入/装配，跑通现有断言。

- [ ] **Step 7: 更新 src 调用点类名**

对 `src/yuki/bus_server/main.py`：
```python
from yuki.bus import BusHub
...
bus = BusHub(base_port=config.base_port, hwm=config.hwm)
```

对 `src/yuki/perception/main.py`、`src/yuki/cognition/main.py`、`src/yuki/interaction/main.py`、`src/yuki/recorder/cli.py`、`src/yuki/supervisor/main.py`：`from yuki.bus import MessageBus` → `from yuki.bus import BusNode`；`MessageBus(base_port=..., role=..., hwm=...)` → `BusNode(base_port=..., hwm=...)`（去掉 `role` 参数）。`supervisor/main.py` 第 51 行 `bus = MessageBus(...)` → `bus = BusNode(base_port=config.base_port, hwm=config.hwm)`。

- [ ] **Step 8: 运行 bus/health 相关测试**

Run: `python -m pytest tests/test_bus.py tests/test_bus_faults.py tests/test_health.py -v`
Expected: 全 PASS。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: split MessageBus into BusHub and BusNode"
```

---

### Task 3: ShutdownManager 优先级清理（#9）

**Files:**
- Modify: `src/yuki/shutdown.py`
- Test: `tests/test_shutdown.py`

**Interfaces:**
- Produces: `ShutdownManager.register_cleanup(name, fn, priority=0)`、`run_cleanups()`（按 priority 逆序，异常吞掉）。Task 6 `ProcessAgent.run` 调用 `run_cleanups()`。

- [ ] **Step 1: 追加失败测试到 `tests/test_shutdown.py`**

```python
def test_run_cleanups_executes_in_reverse_priority_order():
    mgr = ShutdownManager()
    order = []
    mgr.register_cleanup("low", lambda: order.append("low"), priority=10)
    mgr.register_cleanup("mid", lambda: order.append("mid"), priority=5)
    mgr.register_cleanup("high", lambda: order.append("high"), priority=0)
    mgr.run_cleanups()
    assert order == ["low", "mid", "high"]


def test_run_cleanups_swallows_handler_errors():
    mgr = ShutdownManager()
    called = []

    def boom():
        raise RuntimeError("cleanup failed")

    mgr.register_cleanup("boom", boom, priority=0)
    mgr.register_cleanup("ok", lambda: called.append(1), priority=1)
    mgr.run_cleanups()
    assert called == [1]
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_shutdown.py -v`
Expected: FAIL（`AttributeError: 'ShutdownManager' object has no attribute 'register_cleanup'`）。

- [ ] **Step 3: 扩展 `src/yuki/shutdown.py`**

```python
import signal
import threading
from typing import Callable


class ShutdownManager:
    """注册 SIGINT/SIGTERM/SIGBREAK，提供优雅关闭事件与优先级清理。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._cleanups: list[tuple[int, str, Callable[[], None]]] = []

    def register_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass

    def register_cleanup(self, name: str, fn: Callable[[], None], priority: int = 0) -> None:
        self._cleanups.append((priority, name, fn))

    def run_cleanups(self) -> None:
        for _, _, fn in sorted(self._cleanups, key=lambda item: item[0], reverse=True):
            try:
                fn()
            except Exception:
                pass

    def _handle(self, signum, frame) -> None:
        self._event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def request_shutdown(self) -> None:
        self._event.set()
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_shutdown.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/test_shutdown.py src/yuki/shutdown.py
git commit -m "feat: add priority-ordered cleanup registry to ShutdownManager"
```

---

### Task 4: 健康检查 HealthReporter（#5）

**Files:**
- Modify: `src/yuki/health.py`（重写）
- Create: `tests/fakes.py`、`tests/__init__.py`（共享 FakeBus，供本任务及后续使用）
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `BusNode.error_count`（Task 2）、`Topics.HEARTBEAT`（Task 1）。
- Produces: `HealthStatus(ok, detail)` dataclass；`HealthReporter(bus, process, heartbeat_interval=5.0)` 方法 `register_component(name, check)`、`collect() -> HealthResult`、`start()`、`stop()`。Task 6 依赖 `ProcessAgent.health` 及组件检查。

- [ ] **Step 1: 创建 `tests/fakes.py` 与 `tests/__init__.py`**

`tests/__init__.py`：空文件。

`tests/fakes.py`：

```python
class FakeBus:
    """镜像 BusNode 语义：多 handler + 前缀匹配 + 同步 service map。

    publish 仅记录不派发（生产为异步）；测试手动触发 handler。
    """

    def __init__(self):
        self.published = []
        self.subscriptions: dict[str, list] = {}
        self.services = {}
        self._error_count = 0
        self.closed = False

    def subscribe(self, prefix, handler):
        self.subscriptions.setdefault(prefix, []).append(handler)

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def respond(self, service, handler):
        self.services[service] = handler

    def request(self, service, payload, timeout_ms=2000):
        handler = self.services.get(service)
        if handler is None:
            raise RuntimeError(f"service not found: {service}")
        return handler(payload)

    @property
    def error_count(self):
        return self._error_count

    def close(self):
        self.closed = True
```

- [ ] **Step 2: 重写 `tests/test_health.py`（先红）**

```python
import threading
import time

import pytest

from yuki.health import HealthReporter, HealthStatus
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeTickingBus(FakeBus):
    """心跳线程使用 .wait()，用带 timeout 的 Event 语义的 stop flag。"""

    def __init__(self, heartbeat_interval=0.05):
        super().__init__()
        self._heartbeat_interval = heartbeat_interval


def test_collect_reports_process_and_uptime():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")
    data = reporter.collect()
    assert data["process"] == "cognition"
    assert data["pid"] > 0
    assert data["uptime_s"] >= 0
    assert data["error_count"] == 0
    assert data["healthy"] is True
    assert data["components"] == {}


def test_collect_aggregates_component_health():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")
    reporter.register_component("vlm", lambda: HealthStatus(True, {"loaded": True}))
    reporter.register_component("stt", lambda: HealthStatus(False, {"reason": "not loaded"}))
    data = reporter.collect()
    assert data["components"]["vlm"] == {"ok": True, "detail": {"loaded": True}}
    assert data["components"]["stt"] == {"ok": False, "detail": {"reason": "not loaded"}}
    assert data["healthy"] is False


def test_collect_marks_unhealthy_when_check_raises():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")

    def boom():
        raise RuntimeError("check failed")

    reporter.register_component("broken", boom)
    data = reporter.collect()
    assert data["healthy"] is False
    assert data["components"]["broken"]["ok"] is False


def test_start_registers_health_service_and_publishes_heartbeat():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="perception", heartbeat_interval=0.05)
    reporter.start()
    assert "health/perception" in bus.services
    deadline = time.time() + 1.5
    while time.time() < deadline:
        heartbeats = [p for t, p in bus.published if t == Topics.HEARTBEAT]
        if heartbeats:
            break
        time.sleep(0.02)
    assert heartbeats, "expected at least one heartbeat"
    assert heartbeats[0]["process"] == "perception"
    assert heartbeats[0]["healthy"] is True
    reporter.stop()


def test_health_service_returns_collect_result():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="interaction")
    reporter.register_component("tts", lambda: HealthStatus(True))
    reporter.start()
    try:
        result = bus.request("health/interaction", {}, timeout_ms=1000)
        assert result["process"] == "interaction"
        assert result["components"]["tts"]["ok"] is True
    finally:
        reporter.stop()
```

- [ ] **Step 3: 运行验证失败**

Run: `python -m pytest tests/test_health.py -v`
Expected: FAIL（`ImportError` 或 `AttributeError: 'HealthReporter'`）。

- [ ] **Step 4: 重写 `src/yuki/health.py`**

```python
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from yuki.bus import BusNode
from yuki.topics import Topics


@dataclass
class HealthStatus:
    ok: bool
    detail: dict = field(default_factory=dict)


class HealthReporter:
    """进程级健康聚合：组件检查 + 心跳发布 + health/{process} REQ/REP。"""

    def __init__(
        self,
        bus: BusNode,
        process: str,
        heartbeat_interval: float = 5.0,
    ) -> None:
        self._bus = bus
        self._process = process
        self._interval = heartbeat_interval
        self._components: dict[str, Callable[[], HealthStatus]] = {}
        self._started_at = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register_component(self, name: str, check: Callable[[], HealthStatus]) -> None:
        self._components[name] = check

    def collect(self) -> dict:
        components: dict[str, dict] = {}
        healthy = True
        for name, check in self._components.items():
            try:
                status = check()
            except Exception:
                status = HealthStatus(False, {"error": "check raised"})
            components[name] = {"ok": status.ok, "detail": status.detail}
            healthy = healthy and status.ok
        return {
            "process": self._process,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 2),
            "error_count": self._bus.error_count,
            "healthy": healthy,
            "components": components,
        }

    def start(self) -> None:
        self._bus.respond(f"health/{self._process}", lambda payload: self.collect())
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                data = self.collect()
                self._bus.publish(Topics.HEARTBEAT, {
                    "process": data["process"],
                    "ts": time.time(),
                    "healthy": data["healthy"],
                    "components": data["components"],
                })
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
```

注意：原 `register_health_service` 删除，调用点（各 `main.py`）在 Task 6 由 agent 接管，本任务结束时 main.py 里对 `register_health_service` 的调用会 ImportError——**本任务 Step 5 一并移除这些调用**。

- [ ] **Step 5: 从各 main.py 移除 `register_health_service` 调用与导入**

对 `src/yuki/perception/main.py`、`src/yuki/cognition/main.py`、`src/yuki/interaction/main.py`：删除 `from yuki.health import register_health_service` 与 `register_health_service(bus, "xxx")` 行。

- [ ] **Step 6: 运行验证通过**

Run: `python -m pytest tests/test_health.py -v`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: add HealthReporter with component checks and heartbeat"
```

---

### Task 5: Config 嵌套（#8）

**Files:**
- Modify: `src/yuki/config.py`
- Modify: `config.example.yaml`
- Modify: `src/yuki/supervisor/main.py`（env 命名 + 嵌套读取）
- Modify: `tests/test_e2e.py`（env 命名）
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Config(persona_name, bus: BusConfig, logging: LoggingConfig, supervisor: SupervisorConfig, health: HealthConfig)`；`BusConfig(base_port, hwm)`；`LoggingConfig(level)`；`SupervisorConfig(restart_base_delay, restart_max_delay, restart_window, restart_max_per_window)`；`HealthConfig(timeout_ms, heartbeat_interval_s)`。**移除 `bus_role`**。Task 6 依赖 `config.bus.base_port/hwm`、`config.health.heartbeat_interval_s`；Task 9 依赖 `config.supervisor.*`。

- [ ] **Step 1: 重写 `tests/test_config.py`（先红）**

```python
import pytest
from pydantic import ValidationError

from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.persona_name == "yuki"
    assert config.bus.base_port == 5555
    assert config.bus.hwm == 1000
    assert config.logging.level == "INFO"
    assert config.supervisor.restart_base_delay == 1.0
    assert config.supervisor.restart_max_delay == 60.0
    assert config.supervisor.restart_window == 600
    assert config.supervisor.restart_max_per_window == 5
    assert config.health.timeout_ms == 2000
    assert config.health.heartbeat_interval_s == 5.0


def test_from_env_merges_env_overrides(monkeypatch):
    monkeypatch.setenv("YUKI_BUS_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_BUS_HWM", "500")
    monkeypatch.setenv("YUKI_LOGGING_LEVEL", "DEBUG")
    config = Config.load(None)
    assert config.bus.base_port == 7000
    assert config.bus.hwm == 500
    assert config.logging.level == "DEBUG"


def test_yaml_then_env_merge(tmp_path, monkeypatch):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8")
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(yaml_file)
    assert config.bus.base_port == 8000  # 来自 YAML
    assert config.bus.hwm == 300         # env 覆盖 YAML
    assert config.logging.level == "INFO"  # 默认


def test_validation_rejects_bad_port():
    with pytest.raises(ValidationError):
        Config(bus={"base_port": 99})


def test_load_autodiscovers_config_yaml_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(None)
    assert config.bus.base_port == 8000
    assert config.bus.hwm == 300
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL。

- [ ] **Step 3: 重写 `src/yuki/config.py`**

```python
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class BusConfig(BaseModel):
    base_port: int = Field(5555, ge=1024, le=65535)
    hwm: int = Field(1000, ge=1)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class SupervisorConfig(BaseModel):
    restart_base_delay: float = 1.0
    restart_max_delay: float = 60.0
    restart_window: int = 600
    restart_max_per_window: int = 5


class HealthConfig(BaseModel):
    timeout_ms: int = 2000
    heartbeat_interval_s: float = 5.0


class Config(BaseModel):
    persona_name: str = "yuki"
    bus: BusConfig = Field(default_factory=BusConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)

    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "Config":
        data: dict = {}
        path = Path(config_file) if config_file else None
        if path is None:
            default = Path("config.yaml")
            if default.exists():
                path = default
        if path is not None and path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data.update(yaml.safe_load(fh) or {})
        cls._apply_env("persona_name", "PERSONA", data)
        for section_name, section_cls in (
            ("bus", BusConfig),
            ("logging", LoggingConfig),
            ("supervisor", SupervisorConfig),
            ("health", HealthConfig),
        ):
            section = data.setdefault(section_name, {})
            for field_name in section_cls.model_fields:
                cls._apply_env(field_name, f"{section_name.upper()}_{field_name.upper()}", section, section_cls)
        return cls(**data)

    @classmethod
    def _apply_env(cls, field_name: str, env_suffix: str, target: dict, model_cls=None) -> None:
        env_key = f"YUKI_{env_suffix}"
        if env_key not in os.environ:
            return
        raw = os.environ[env_key]
        if model_cls is None:
            annotation = cls.model_fields[field_name].annotation
        else:
            annotation = model_cls.model_fields[field_name].annotation
        if annotation is bool:
            target[field_name] = raw.lower() in ("1", "true", "yes")
            return
        try:
            target[field_name] = annotation(raw)
        except (TypeError, ValueError):
            target[field_name] = raw

    @classmethod
    def from_env(cls) -> "Config":
        return cls.load(None)
```

- [ ] **Step 4: 重写 `config.example.yaml`**

```yaml
# Yuki Agent 配置样例。复制为 config.yaml 使用；环境变量 YUKI_<SECTION>_<FIELD> 覆盖同级项。
persona_name: yuki
bus:
  base_port: 5555
  hwm: 1000
logging:
  level: INFO
supervisor:
  restart_base_delay: 1.0
  restart_max_delay: 60.0
  restart_window: 600
  restart_max_per_window: 5
health:
  timeout_ms: 2000
  heartbeat_interval_s: 5.0
```

- [ ] **Step 5: 更新 `src/yuki/supervisor/main.py`**

- `config.restart_base_delay` → `config.supervisor.restart_base_delay`（其余 restart_* 同理）。
- `config.health_timeout_ms` → `config.health.timeout_ms`。
- env 构建改为：

```python
    env = dict(os.environ)
    env["YUKI_BUS_BASE_PORT"] = str(config.bus.base_port)
    env["YUKI_BUS_HWM"] = str(config.bus.hwm)
```

（删除 `YUKI_BUS_ROLE` 注入。）

- [ ] **Step 6: 更新 `tests/test_e2e.py`**

`env["YUKI_BASE_PORT"] = str(port)` → `env["YUKI_BUS_BASE_PORT"] = str(port)`。

- [ ] **Step 7: 运行 config 相关测试**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: nest Config into bus/logging/supervisor/health sections"
```

---

### Task 6: ProcessAgent 框架 + PerceptionAgent（#2/#3）

**Files:**
- Create: `src/yuki/process.py`
- Create: `src/yuki/perception/agent.py`
- Modify: `src/yuki/perception/main.py`
- Test: `tests/test_process.py`、`tests/test_perception_smoke.py`

**Interfaces:**
- Consumes: `BusNode`、`ShutdownManager.register_cleanup/run_cleanups`、`HealthReporter`、嵌套 `Config`。
- Produces: `ProcessAgent`（`name`、`config/bus/shutdown/health`、`setup()/teardown()/loop()/health_components()/run()`、`_make_bus()`、`register_health` 类属性）；`PerceptionAgent(config, *, bus, shutdown, capture, monitor, audio, scroll_hook, strategy, foreground_hwnd)`。Task 7/8/9 复用 `ProcessAgent`。

- [ ] **Step 1: 创建 `tests/test_process.py`（先红）**

```python
import threading

from yuki.config import Config
from yuki.health import HealthStatus
from yuki.process import ProcessAgent
from yuki.shutdown import ShutdownManager

from tests.fakes import FakeBus


class FakeAgent(ProcessAgent):
    name = "fake"

    def __init__(self, config):
        super().__init__(config)
        self.events = []
        self.components = {"comp": lambda: HealthStatus(True)}

    def setup(self):
        self.events.append("setup")

    def teardown(self):
        self.events.append("teardown")

    def health_components(self):
        return self.components


def test_agent_run_orders_lifecycle_and_closes_bus():
    bus = FakeBus()
    shutdown = ShutdownManager()
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert agent.events == ["setup", "teardown"]
    assert bus.closed is True


def test_agent_teardown_runs_even_when_setup_raises():
    bus = FakeBus()
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.events = []

    def boom():
        raise RuntimeError("setup failed")

    agent.setup = boom
    try:
        agent.run(register_signals=False)
    except RuntimeError:
        pass
    assert "teardown" in agent.events
    assert bus.closed is True


def test_agent_run_runs_cleanups():
    bus = FakeBus()
    shutdown = ShutdownManager()
    order = []
    shutdown.register_cleanup("x", lambda: order.append("cleanup"), priority=0)
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert order == ["cleanup"]


def test_agent_health_started_only_when_register_health():
    class NoHealthAgent(FakeAgent):
        register_health = False

    bus = FakeBus()
    shutdown = ShutdownManager()
    agent = NoHealthAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert "health/fake" not in bus.services
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_process.py -v`
Expected: FAIL（`ModuleNotFoundError: yuki.process`）。

- [ ] **Step 3: 创建 `src/yuki/process.py`**

```python
from abc import ABC, abstractmethod
from typing import Callable

from yuki.bus import BusNode
from yuki.config import Config
from yuki.health import HealthReporter, HealthStatus
from yuki.shutdown import ShutdownManager


class ProcessAgent(ABC):
    """进程生命周期框架：信号 → 健康 → setup → loop → teardown → 清理 → 关总线。"""

    name: str = "process"
    register_health: bool = True

    def __init__(
        self,
        config: Config,
        *,
        bus: BusNode | None = None,
        shutdown: ShutdownManager | None = None,
    ) -> None:
        self.config = config
        self.bus = bus or self._make_bus()
        self.shutdown = shutdown or ShutdownManager()
        self.health = HealthReporter(
            self.bus,
            process=self.name,
            heartbeat_interval=config.health.heartbeat_interval_s,
        )

    def _make_bus(self) -> BusNode:
        return BusNode(base_port=self.config.bus.base_port, hwm=self.config.bus.hwm)

    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def teardown(self) -> None: ...

    def health_components(self) -> dict[str, Callable[[], HealthStatus]]:
        return {}

    def loop(self) -> None:
        while not self.shutdown.shutdown_requested:
            self.shutdown.wait(timeout=1.0)

    def run(self, *, register_signals: bool = True) -> None:
        if register_signals:
            self.shutdown.register_signal_handlers()
        if self.register_health:
            for comp_name, check in self.health_components().items():
                self.health.register_component(comp_name, check)
            self.health.start()
        try:
            self.setup()
            self.loop()
        finally:
            self.teardown()
            if self.register_health:
                self.health.stop()
            self.shutdown.run_cleanups()
            self.bus.close()
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_process.py -v`
Expected: PASS。

- [ ] **Step 5: 重写 `tests/test_perception_smoke.py`（改 PerceptionAgent 装配）**

把 `build_perception(bus, config, capture=..., ...)` 改为：

```python
agent = PerceptionAgent(
    config,
    bus=bus,
    capture=capture,
    monitor=monitor,
    audio=audio,
    scroll_hook=scroll_hook,
    strategy=strategy,
)
agent.setup()
# ... 断言 bus.services["frame"] 等 ...
agent.teardown()
```

具体四个用例：
- `test_build_perception_wires_components`：改名为 `test_perception_agent_setup_wires_components`，装配后断言 `bus.services["frame"]` 形状、`capture.started`/`monitor.started`/`audio.started`/`scroll_hook.started`，再 `agent.teardown()` 并断言 `capture.stopped` 等。注意 teardown 逆序：scroll_hook→capture→monitor→audio。
- `test_build_perception_default_constructs`：删除 `monkeypatch.setattr(pm, "_perception_state", {})`（不再有模块全局）；直接 `PerceptionAgent(config, bus=bus, capture=FakeCapture(), monitor=FakeMonitor(), audio=FakeAudio(), scroll_hook=FakeScrollHook()).setup()`。
- `test_build_perception_default_strategy_gates_on_scroll`：同样去掉 `_perception_state` monkeypatch；`ScrollHook` 注入用 `agent` 构造参数 `scroll_hook=RecordingScrollHook`（RecordingScrollHook 需要接收 `on_scroll`）——在 setup 内 ScrollIdleDetector 会实例化，测试把 `strategy` 与 `scroll_hook` 都注入。
- `test_build_perception_registers_frame_service_when_no_capture`：`foreground_hwnd=0` 传入 `PerceptionAgent(..., foreground_hwnd=0)`，其余注入 fake。

- [ ] **Step 6: 创建 `src/yuki/perception/agent.py`**

```python
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.perception.audio import AudioCapture
from yuki.perception.capture import FrameStrategy, NullCapture, WgcCapture, make_frame_service
from yuki.perception.scroll import ScrollHook, ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor, make_monitor
from yuki.process import ProcessAgent


class PerceptionAgent(ProcessAgent):
    name = "perception"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 capture=None, monitor=None, audio=None, scroll_hook=None,
                 strategy=None, foreground_hwnd: int | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._capture = capture
        self._monitor = monitor
        self._audio = audio
        self._scroll_hook = scroll_hook
        self._strategy = strategy
        self._foreground_hwnd = foreground_hwnd
        self._components: dict = {}

    def setup(self) -> None:
        detector = SensitiveDetector()
        idle = ScrollIdleDetector(idle_ms=300)
        strategy = self._strategy or FrameStrategy(sensitive=detector, idle=idle, require_idle=True)

        gate_hwnd = 0
        capture = self._capture
        if capture is None:
            hwnd = self._foreground_hwnd
            if hwnd is None:
                try:
                    import win32gui
                    hwnd = win32gui.GetForegroundWindow()
                except Exception:
                    hwnd = 0
            gate_hwnd = hwnd
            capture = WgcCapture(hwnd) if hwnd else NullCapture()
        elif isinstance(capture, WgcCapture):
            gate_hwnd = capture.window_hwnd

        monitor = self._monitor or make_monitor(self.bus, probe=ForegroundProbe())
        audio = self._audio or AudioCapture(self.bus)
        scroll_hook = self._scroll_hook or ScrollHook(on_scroll=idle.on_scroll_activity)

        make_frame_service(self.bus, capture, strategy, hwnd=gate_hwnd)

        self._components = {
            "capture": capture,
            "monitor": monitor,
            "audio": audio,
            "scroll_hook": scroll_hook,
        }
        monitor.start()
        audio.start()
        capture.start()
        scroll_hook.start()

    def teardown(self) -> None:
        for key in ("scroll_hook", "capture", "monitor", "audio"):
            comp = self._components.get(key)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass

    def health_components(self):
        return {
            "audio": self._health_audio,
            "capture": self._health_capture,
            "monitor": self._health_monitor,
            "scroll_hook": self._health_scroll,
        }

    def _health_audio(self) -> HealthStatus:
        stream = getattr(self._components.get("audio"), "_stream", None)
        return HealthStatus(stream is not None, {"stream_active": stream is not None})

    def _health_capture(self) -> HealthStatus:
        capture = self._components.get("capture")
        ok = capture is not None and capture.on_frame is not None
        return HealthStatus(ok, {"frame_registered": ok})

    def _health_monitor(self) -> HealthStatus:
        monitor = self._components.get("monitor")
        thread = getattr(monitor, "_thread", None)
        alive = thread is not None and thread.is_alive()
        return HealthStatus(alive, {"thread_alive": alive})

    def _health_scroll(self) -> HealthStatus:
        scroll = self._components.get("scroll_hook")
        return HealthStatus(scroll is not None, {"installed": scroll is not None})
```

- [ ] **Step 7: 收敛 `src/yuki/perception/main.py`**

```python
from yuki.config import Config
from yuki.perception.agent import PerceptionAgent


def main() -> None:
    PerceptionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
```

（删除 `build_perception`/`_perception_state`/`register_health_service` 全部旧逻辑。）

- [ ] **Step 8: 运行感知相关测试**

Run: `python -m pytest tests/test_process.py tests/test_perception_smoke.py tests/perception -v`
Expected: 全 PASS（`tests/perception` 下其余用例不受影响）。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: add ProcessAgent framework and PerceptionAgent"
```

---

### Task 7: CognitionAgent + 数据流接入 + 删死代码（#2/#6/#12 死代码部分）

**Files:**
- Create: `src/yuki/cognition/agent.py`
- Modify: `src/yuki/cognition/main.py`
- Modify: `src/yuki/cognition/l1_responder.py`（订阅 SITUATION_UPDATE，存 context）
- Modify: `src/yuki/cognition/pipeline.py`（加 `warmup_vlm()`）
- Delete: `src/yuki/cognition/responder.py`、`tests/test_responder.py`
- Test: `tests/cognition/test_l1_responder.py`、`tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `Topics.SITUATION_UPDATE`、`build_pipeline`/`build_l1_responder`、`SituationUpdatePayload`。
- Produces: `CognitionAgent(config, *, bus, shutdown, pipeline, l1, vlm, stt, frame_client, sensitive_filter, speech_buffer)`；L1Responder 新增属性 `_context`（`SituationUpdatePayload | None`）。

- [ ] **Step 1: 更新 `tests/cognition/test_l1_responder.py`（先红，加 context 用例）**

```python
import pytest

from yuki.cognition.l1_responder import L1Responder, build_l1_responder
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def __init__(self):
        self.seen_contexts = []

    def reply(self, text, context=None):
        self.seen_contexts.append(context)
        return f"reply:{text}"


def test_awake_triggers_l1_reply():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_utterance_triggers_l1_reply_with_text():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.USER_UTTERANCE][0](
        Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    replies = [p for t, p in bus.published if t == Topics.REPLY]
    assert replies and replies[0]["text"] == "reply:你好"


def test_situation_update_stores_context_and_feeds_reply():
    bus = FakeBus()
    l1 = FakeL1()
    responder = build_l1_responder(bus, l1=l1)
    situation = {"source_id": "https://example.com", "scroll_band": "0-25",
                 "topic": "量子计算", "summary": "介绍", "content_type": "web",
                 "key_points": ["a"], "sensitive": False, "degraded": False,
                 "reason": "", "ts": 0.0}
    bus.subscriptions[Topics.SITUATION_UPDATE][0](Topics.SITUATION_UPDATE, situation)
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert l1.seen_contexts and l1.seen_contexts[0] == situation
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_l1_responder.py -v`
Expected: FAIL（`KeyError: 'event/perception/situation_update'` 或断言失败）。

- [ ] **Step 3: 更新 `src/yuki/cognition/l1_responder.py`**

```python
import time

from yuki.cognition.l1 import L1Engine
from yuki.logger import get_logger
from yuki.payloads import SituationUpdatePayload
from yuki.topics import Topics

logger = get_logger("yuki.cognition.l1_responder")


class L1Responder:
    """L1 快答消费者：订阅感知事件 + awake，产出 event/reply。

    职责边界：感知管线只产理解事件；本组件消费并回复。
    SITUATION_UPDATE 仅作为 context 注入，不自动回复——
    主动评论（是否开口/何时开口）留待 Brain 阶段决策。  # TODO(Brain): 主动评论决策
    Brain 阶段直接替换本组件（同样的订阅，更聪明的 Brain）。
    """

    def __init__(self, l1: L1Engine, bus) -> None:
        self._l1 = l1
        self._bus = bus
        self._context: SituationUpdatePayload | None = None

    def on_situation_update(self, topic: str, payload: dict) -> None:
        self._context = payload

    def on_awake(self, topic: str, payload: dict) -> None:
        reply = self._l1.reply("", context=self._context)
        self._publish(reply)

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        reply = self._l1.reply(text, context=self._context)
        self._publish(reply)

    def _publish(self, text: str) -> None:
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_l1_responder(bus, *, l1=None) -> L1Responder:
    responder = L1Responder(l1=l1 or L1Engine(), bus=bus)
    bus.subscribe(Topics.AWAKE, responder.on_awake)
    bus.subscribe(Topics.USER_UTTERANCE, responder.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, responder.on_situation_update)
    return responder
```

- [ ] **Step 4: 给 `PerceptionPipeline` 加 `warmup_vlm()`**

在 `src/yuki/cognition/pipeline.py` 的类内追加：

```python
    def warmup_vlm(self) -> None:
        self._vlm.warmup()
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/cognition/test_l1_responder.py -v`
Expected: PASS。

- [ ] **Step 6: 重写 `tests/cognition/test_cognition.py`**

删除 `build_cognition` 相关三个用例，改为 CognitionAgent 装配用例：

```python
from yuki.cognition.agent import CognitionAgent
from yuki.config import Config
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakePipeline:
    def warmup_vlm(self):
        pass


def test_cognition_agent_wires_pipeline_and_responder():
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
    )
    agent.setup()
    assert Topics.AWAKE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    agent.teardown()
```

- [ ] **Step 7: 创建 `src/yuki/cognition/agent.py`**

```python
from yuki.cognition.l1 import L1Engine
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.process import ProcessAgent


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._responder = None

    def setup(self) -> None:
        if self._pipeline is None:
            self._pipeline = build_pipeline(
                self.bus,
                vlm=self._vlm,
                sensitive_filter=self._sensitive_filter,
                stt=self._stt,
                frame_client=self._frame_client,
                speech_buffer=self._speech_buffer,
            )
        self._pipeline.warmup_vlm()  # VLM 后台预热（不可用则降级文本模式）
        self._responder = build_l1_responder(self.bus, l1=self._l1 or L1Engine())

    def teardown(self) -> None:
        pass

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "l1": self._health_l1,
            "pipeline": self._health_pipeline,
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(False, {"reason": "no_vlm"})
        return HealthStatus(vlm._loaded, {"loaded": vlm._loaded})

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        return HealthStatus(stt is not None, {"installed": stt is not None})

    def _health_l1(self) -> HealthStatus:
        return HealthStatus(self._responder is not None, {"installed": self._responder is not None})

    def _health_pipeline(self) -> HealthStatus:
        frame_client = getattr(self._pipeline, "_frame_client", None) if self._pipeline else None
        ok = frame_client is not None and hasattr(frame_client, "get_latest")
        return HealthStatus(ok, {"frame_client_available": ok})
```

- [ ] **Step 8: 收敛 `src/yuki/cognition/main.py`**

```python
from yuki.config import Config
from yuki.cognition.agent import CognitionAgent


def main() -> None:
    CognitionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: 删除死代码**

`Remove-Item src/yuki/cognition/responder.py, tests/test_responder.py`

- [ ] **Step 10: 运行认知相关测试**

Run: `python -m pytest tests/cognition tests/test_responder.py -v`
Expected: 全 PASS（`tests/test_responder.py` 已删除，pytest 无该路径文件即通过）。

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat: add CognitionAgent, wire situation context into L1, remove dead code"
```

---

### Task 8: InteractionAgent + 交互层桩（#10）

**Files:**
- Create: `src/yuki/interaction/agent.py`（含 TTS/FocusManager/VolumeController 桩）
- Modify: `src/yuki/interaction/main.py`
- Test: `tests/interaction/test_interaction.py`

**Interfaces:**
- Consumes: `Topics.AWAKE/REPLY`、`HotkeyManager`、`AwakePayload`/`ReplyPayload`。
- Produces: `InteractionAgent(config, *, bus, shutdown, hotkeys, tts, focus_manager, volume_controller)`；`TTS.speak(text)`（stdout 输出 `[yuki] {text}`）；`FocusManager.is_interruptible() -> bool`；`VolumeController.level() -> str`。

- [ ] **Step 1: 重写 `tests/interaction/test_interaction.py`（先红）**

```python
from yuki.config import Config
from yuki.interaction.agent import InteractionAgent
from yuki.interaction.hotkey import HotkeyManager
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeHotkeys:
    def __init__(self):
        self.handler = None

    def register(self, name, handler):
        self.handler = handler

    def trigger(self, name):
        self.handler()


class FakeTTS:
    def __init__(self):
        self.said = []

    def speak(self, text):
        self.said.append(text)


def test_hotkey_manager_register_trigger():
    calls = []
    hk = HotkeyManager()
    hk.register("trigger", lambda: calls.append("x"))
    hk.trigger("trigger")
    assert calls == ["x"]


def test_interaction_agent_publishes_awake_on_trigger():
    bus = FakeBus()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=FakeTTS())
    agent.setup()
    agent._hotkeys.trigger("trigger")
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.AWAKE
    assert payload["source"] == "hotkey"
    agent.teardown()


def test_interaction_agent_reply_feeds_tts():
    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    bus.subscriptions[Topics.REPLY][0](Topics.REPLY, {"text": "你好", "ts": 0.0})
    assert tts.said == ["你好"]
    agent.teardown()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/interaction/test_interaction.py -v`
Expected: FAIL（`ModuleNotFoundError: yuki.interaction.agent`）。

- [ ] **Step 3: 创建 `src/yuki/interaction/agent.py`**

```python
import sys
import time

from yuki.config import Config
from yuki.health import HealthStatus
from yuki.interaction.hotkey import HotkeyManager
from yuki.payloads import ReplyPayload
from yuki.process import ProcessAgent
from yuki.topics import Topics


class TTS:
    """TTS 合成桩：控制台输出。Phase 4 由真实语音合成替换。"""

    def speak(self, text: str) -> None:
        print(f"[yuki] {text}", flush=True)


class FocusManager:
    """打断控制桩：恒可打断。Phase 4 实现抢话检测。"""

    def is_interruptible(self) -> bool:
        return True


class VolumeController:
    """三档位桩：恒 normal。Phase 4 实现安静/普通/活跃切换。"""

    def level(self) -> str:
        return "normal"


class InteractionAgent(ProcessAgent):
    name = "interaction"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 hotkeys=None, tts=None, focus_manager=None, volume_controller=None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._hotkeys = hotkeys or HotkeyManager()
        self._tts = tts or TTS()
        self._focus_manager = focus_manager or FocusManager()
        self._volume_controller = volume_controller or VolumeController()
        self._tts_is_active = False

    def setup(self) -> None:
        def on_reply(topic: str, payload: dict) -> None:
            self._tts.speak(payload["text"])

        def trigger_call() -> None:
            self.bus.publish(Topics.AWAKE, {"source": "hotkey", "ts": time.time()})

        self.bus.subscribe(Topics.REPLY, on_reply)
        self._hotkeys.register("trigger", trigger_call)

        if "--trigger-after" in sys.argv:
            import threading
            delay = float(sys.argv[sys.argv.index("--trigger-after") + 1])

            def delayed() -> None:
                time.sleep(delay)
                self._hotkeys.trigger("trigger")

            threading.Thread(target=delayed, daemon=True).start()

    def teardown(self) -> None:
        pass

    def health_components(self):
        return {
            "tts": lambda: HealthStatus(True, {"output": "console"}),
            "hotkeys": lambda: HealthStatus(True, {"installed": True}),
        }
```


- [ ] **Step 4: 收敛 `src/yuki/interaction/main.py`**

```python
from yuki.config import Config
from yuki.interaction.agent import InteractionAgent


def main() -> None:
    InteractionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/interaction/test_interaction.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: add InteractionAgent with TTS/FocusManager/VolumeController stubs"
```

---

### Task 9: BusServerAgent + RecorderAgent + main 收敛（#2）

**Files:**
- Create: `src/yuki/bus_server/agent.py`、`src/yuki/recorder/agent.py`
- Modify: `src/yuki/bus_server/main.py`、`src/yuki/recorder/cli.py`
- Modify: `src/yuki/supervisor/main.py`（supervisor 进程保持手写循环，仅替换 bus 与 config 字段——Task 2/5 已处理）
- Test: `tests/recorder/test_cli.py`

**Interfaces:**
- Consumes: `BusHub`、`ProcessAgent`、嵌套 `Config`。
- Produces: `BusServerAgent(config)`（`register_health=False`）；`RecorderAgent(config, *, session, grabber, interval_sec)`（`loop()` 覆写）。

- [ ] **Step 1: 创建 `src/yuki/bus_server/agent.py`**

```python
from yuki.bus import BusHub
from yuki.process import ProcessAgent


class BusServerAgent(ProcessAgent):
    name = "bus_server"
    register_health = False

    def _make_bus(self):
        return BusHub(base_port=self.config.bus.base_port, hwm=self.config.bus.hwm)

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass
```

- [ ] **Step 2: 收敛 `src/yuki/bus_server/main.py`**

```python
from yuki.bus_server.agent import BusServerAgent
from yuki.config import Config


def main() -> None:
    BusServerAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 创建 `src/yuki/recorder/agent.py`**

```python
import time

from yuki.config import Config
from yuki.process import ProcessAgent
from yuki.topics import Topics


class RecorderAgent(ProcessAgent):
    name = "recorder"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 session=None, grabber=None, interval_sec: float = 1.0) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._session = session
        self._grabber = grabber
        self._interval_sec = interval_sec

    def setup(self) -> None:
        def on_event(topic: str, payload: dict) -> None:
            self._session.record_event(topic, payload)

        self.bus.subscribe("event/", on_event)

    def loop(self) -> None:
        next_grab = time.time()
        while not self.shutdown.shutdown_requested:
            now = time.time()
            if now >= next_grab and self._grabber is not None:
                self._session.save_frame(self._grabber())
                next_grab = now + self._interval_sec
            self.shutdown.wait(timeout=0.05)

    def teardown(self) -> None:
        self._session.close()
```

- [ ] **Step 4: 重写 `src/yuki/recorder/cli.py`**

```python
import argparse
import io
from pathlib import Path

from PIL import ImageGrab

from yuki.config import Config
from yuki.recorder.agent import RecorderAgent
from yuki.recorder.session import Session


def grab_frame() -> bytes:
    image = ImageGrab.grab()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a browsing session: frames + events, no audio.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frame grabs")
    parser.add_argument("--no-frames", action="store_true", help="record events only")
    args = parser.parse_args()

    config = Config.from_env()
    session = Session(Path(args.output_dir))
    grabber = None if args.no_frames else grab_frame
    RecorderAgent(config, session=session, grabber=grabber, interval_sec=args.interval).run()


if __name__ == "__main__":
    main()
```

注意：`Session(Path(...))` 签名需保留与现状一致（`Session` 构造函数接受 `Path`，`session_id` 可选——见 Task 10 Step 5 的既有测试用法）。

- [ ] **Step 5: 重写 `tests/recorder/test_cli.py`**

```python
import json

import pytest

from yuki.recorder import cli
from yuki.recorder.agent import RecorderAgent
from yuki.recorder.session import Session

from tests.fakes import FakeBus


class FakeShutdown:
    def __init__(self, iterations=3):
        self._iterations = iterations
        self._calls = 0

    def register_signal_handlers(self):
        pass

    @property
    def shutdown_requested(self):
        return self._calls >= self._iterations

    def wait(self, timeout=None):
        self._calls += 1
        return False


class FakeSession:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.closed = False
        self.events = []
        self.frames = []

    def record_event(self, topic, payload):
        self.events.append((topic, payload))

    def save_frame(self, png):
        self.frames.append(png)

    def close(self):
        self.closed = True


def test_recorder_agent_records_events_and_frames():
    bus = FakeBus()
    session = FakeSession("out")
    agent = RecorderAgent(Config(), bus=bus, session=session, grabber=lambda: b"png", interval_sec=0.0)
    agent.shutdown = FakeShutdown(iterations=2)
    agent.setup()
    bus.subscriptions["event/"][0]("event/reply", {"text": "hi", "ts": 0.0})
    assert session.events == [("event/reply", {"text": "hi", "ts": 0.0})]
    agent.loop()
    assert session.frames == [b"png", b"png"]
    agent.teardown()
    assert session.closed is True


def test_main_propagates_run_exception(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["yuki.recorder", "--output-dir", str(tmp_path), "--no-frames"])
    monkeypatch.setattr(
        cli.Config, "from_env", classmethod(lambda cls: Config())
    )
    monkeypatch.setattr(
        cli.RecorderAgent,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("grab failed")),
    )
    with pytest.raises(RuntimeError, match="grab failed"):
        cli.main()
```

说明：`Config.from_env` 桩直接返回真实 `Config()`（嵌套默认值即可，`ProcessAgent.__init__` 需要 `config.health.heartbeat_interval_s`）。RecorderAgent 构造会创建真实 `BusNode`（连接是异步的、无需 hub 在线），对象丢弃时 `__del__` 负责 close。`run` 被替换为抛异常，仅验证异常向上传播。测试文件需 `from yuki.config import Config`。

- [ ] **Step 6: 运行 recorder/bus_server 相关测试**

Run: `python -m pytest tests/recorder tests/test_supervisor_main.py tests/test_smoke.py -v`
Expected: 全 PASS。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: BusServerAgent and RecorderAgent, collapse main.py entrypoints"
```

---

### Task 10: 代码质量修复（#12）

**Files:**
- Modify: `src/yuki/perception/system_monitor.py`（惰性 win32 导入）
- Modify: `src/yuki/perception/capture.py`（锁）
- Modify: `src/yuki/cognition/vlm.py`（import torch）
- Modify: `src/yuki/logger.py`（惰性文件日志）
- Modify: `src/yuki/perception/audio.py`（注释）
- Test: `tests/fakes.py`（共享 FakeBus，Task 4 已建）；更新仍用本地 FakeBus 的测试（`test_pipeline.py`、`test_cognition.py`、`test_perception_smoke.py`、`test_interaction.py` 已改；`test_recorder/test_cli.py` 已改）

- [ ] **Step 1: system_monitor.py 惰性 win32**

把模块顶部：

```python
import win32gui  # noqa: E402
import win32process  # noqa: E402
```

替换为：

```python
try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None


def _noop(*args, **kwargs):
    raise RuntimeError("win32 unavailable")
```

并把 `ForegroundProbe.__init__` 默认参数改为惰性解析：

```python
    def __init__(
        self,
        get_foreground=None,
        get_text=None,
        get_class=None,
        get_pid=None,
        process_name=_default_process_name,
    ) -> None:
        self._get_foreground = get_foreground or (win32gui.GetForegroundWindow if win32gui else _noop)
        self._get_text = get_text or (win32gui.GetWindowText if win32gui else _noop)
        self._get_class = get_class or (win32gui.GetClassName if win32gui else _noop)
        self._get_pid = get_pid or (win32process.GetWindowThreadProcessId if win32process else _noop)
        self._process_name = process_name
```

`probe()` 已 catch 异常返回 None，天然 graceful degrade。

- [ ] **Step 2: capture.py 加锁**

- 顶部：`import threading`。
- `make_frame_service` 内：`latest_lock = threading.Lock()`。
- `on_frame` 内所有对 `latest[...]` 的赋值包进 `with latest_lock:`（三处：敏感黑帧分支、`not capture_ok` 分支无需写、正常分支）。
- handler：

```python
    def handler(payload: dict) -> dict:
        with latest_lock:
            return dict(latest)
```

- [ ] **Step 3: vlm.py import torch**

`with __import__("torch").no_grad():` 改为：

```python
        import torch

        with torch.no_grad():
```

（`import torch` 放 `_infer` 函数内，与 `_load` 同风格。）

- [ ] **Step 4: logger.py 惰性文件日志**

把模块级：

```python
audit_logger = get_file_logger("yuki.audit", "audit.jsonl")
decision_logger = get_file_logger("yuki.decision", "decision.jsonl")
```

替换为：

```python
_audit_logger = None
_decision_logger = None


def get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = get_file_logger("yuki.audit", "audit.jsonl")
    return _audit_logger


def get_decision_logger():
    global _decision_logger
    if _decision_logger is None:
        _decision_logger = get_file_logger("yuki.decision", "decision.jsonl")
    return _decision_logger
```

`logs/` 目录不再在 import 时创建。若存在对 `audit_logger`/`decision_logger` 的引用（当前 src 内无），改用 `get_audit_logger()`/`get_decision_logger()`。

- [ ] **Step 5: audio.py 注释**

`audio.py` `_on_audio` 内 base64 行上方加注释：

```python
        # PCM 经 base64 塞入 protobuf Struct（Struct 仅支持 JSON 值）。
        # 带宽浪费 33%，量级可忽略（20ms/帧 ~426B）；
        # 待 proto 升级为 typed message（bytes 字段）时一并消除。
```

- [ ] **Step 6: 收敛剩余本地 FakeBus**

将仍使用本地 `FakeBus` 的测试文件切换到 `tests/fakes.py` 的共享实现：
- `tests/cognition/test_pipeline.py`：删本地 FakeBus，`from tests.fakes import FakeBus`；若测试依赖 `subscriptions[prefix]` 单 handler 触发，改为 `bus.subscriptions[prefix][0](...)`。
- `tests/cognition/test_cognition.py`（Task 7 已重写）。
- 其余已改文件确认无残留 `class FakeBus` 定义。

- [ ] **Step 7: 运行全仓单元测试（e2e 除外）**

Run: `python -m pytest`
Expected: 全 PASS（e2e 标记自动跳过）。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "fix: lazy win32 import, lock frame state, lazy file loggers, misc quality"
```

---

### Task 11: 全量回归 + e2e

**Files:** 无新改动。

- [ ] **Step 1: 全仓回归**

Run: `python -m pytest -q`
Expected: 全 PASS。

- [ ] **Step 2: e2e 手动验证（交互式桌面会话下）**

Run: `python -m pytest tests/test_e2e.py -m e2e -q`
Expected: PASS（stdout 出现 `[yuki] 我在，你说。`）。

- [ ] **Step 3: 冒烟验证 hub 独立进程**

在真实 shell 中：

```bash
python -m yuki.bus_server
```

Expected: 进程启动无异常；Ctrl+C 优雅退出（BusHub 正常 close）。

- [ ] **Step 4: 手工核对产物**

- `git status` 确认无意外删除/新增。
- 确认 `src/yuki/cognition/topics_ext.py`、`src/yuki/cognition/responder.py`、`tests/test_responder.py`、`tests/cognition/test_topics_ext.py` 已删。
- 确认无残留 `MessageBus`/`TopicsExt`/`_perception_state`/`register_health_service` 引用：`rg -n "MessageBus|TopicsExt|_perception_state|register_health_service|YUKI_BASE_PORT|bus_role" src tests config.example.yaml`

- [ ] **Step 5: Commit（若有遗留）**

```bash
git add -A
git commit -m "test: full regression pass after architecture hardening"
```

---

## Self-Review

**Spec 覆盖：**
- #1 Bus 拆分 → Task 2；#2 ProcessRunner → Task 6-9；#3 PerceptionAgent → Task 6；#4 主题合并 → Task 1；#5 健康 → Task 4；#6 数据流 → Task 7；#7 TypedDict → Task 1；#8 Config → Task 5；#9 关闭清理 → Task 3；#10 InteractionAgent → Task 8；#12 代码质量 → Task 10。无缺口。
- 设计 §10 的"删除 bus_role / supervisor 不再注入 role"→ Task 5 Step 5。
- 设计 §3 的 TTS 桩 stdout 输出（e2e 等价）→ Task 8。

**占位符扫描：** 无 TBD/TODO（Task 8 Step 3 的"修正说明"为最终实现指示，非占位）。Task 6 Step 5 的测试改写以描述+要点给出，Task 9 Step 5 测试有完整代码。

**类型一致性：** `ProcessAgent.register_health`、`_make_bus()`、`HealthReporter(bus, process, heartbeat_interval)`、`Topics.HEARTBEAT`、`BusNode.error_count`、嵌套 Config 字段名在 Task 4/5/6/9 间一致。`L1Responder._context` 类型 `SituationUpdatePayload | None` 与 payloads.py 一致。

**已知风险：** `tests/fakes.py` 依赖 `tests/__init__.py` 使 `from tests.fakes import FakeBus` 在 `--import-mode=importlib` 下可导入（rootdir 已在 sys.path）。若导入失败，改在各测试模块内定义与 fakes.py 相同实现的 FakeBus，或把 fakes 移到 `src/yuki/testing.py`。
