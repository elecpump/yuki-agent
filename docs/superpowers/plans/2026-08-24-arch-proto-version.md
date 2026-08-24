# 协议版本校验 Implementation Plan（架构评审主题 3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 启用 `Envelope.version`（现 `VERSION=1` 定义了但从未校验）：`parse_envelope` 之后检查超版本丢弃 + 告警，`WhichOneof` 分支补未知 kind 告警，REGISTER 控制帧携带版本让 BusHub 能拒绝不兼容节点——未来协议演进时旧进程优雅降级而非静默错乱。

**Architecture:** `codec.py` 暴露 `MAX_SUPPORTED_VERSION` 与 `version_supported(env) -> bool`；`bus.py` 的三个信封消费点（`BusHub._router_loop`、`BusNode._dealer_loop`、`BusNode._run_sub`）解析后校验版本、未知 oneof kind 落 `else` 告警；`BusNode._register_frames` 追加版本帧，`BusHub` REGISTER 解析改为向后兼容读取可选版本帧（缺省按 1 处理）。

**Tech Stack:** Python ≥3.11，protobuf，pyzmq，pytest。无新增运行时依赖。

## Global Constraints

- **向后兼容**：REGISTER 版本帧为可选——缺省视为 version 1，旧节点（不携带版本帧）仍能注册；本计划同时改两端，同仓部署无混合版本。
- 版本检查只发生在信封**已成功解析后**：`parse_envelope` 本身不抛版本错误（DecodeError 语义不变）。
- `version_supported(env) -> bool`：`env.version <= MAX_SUPPORTED_VERSION`；超版本时调用点 `drop + logger.warning`。
- 未知 oneof kind：`kind = envelope.WhichOneof("body")` 的每个 if/elif 链补 `else: logger.warning(...)` 并丢弃该帧（不静默跳过）。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**修改**
- `src/yuki/proto/codec.py` — `MAX_SUPPORTED_VERSION` + `version_supported()`
- `src/yuki/bus.py` — 三处信封消费点版本/oneof 校验 + REGISTER 版本帧
- 测试：`tests/test_proto_codec.py`、`tests/test_bus_faults.py`

---

### Task 1: codec 版本常量与校验函数

**Files:**
- Modify: `src/yuki/proto/codec.py`
- Modify: `tests/test_proto_codec.py`

**Interfaces:**
- Consumes: 无。
- Produces: `MAX_SUPPORTED_VERSION = 1`、`version_supported(env: yuki_pb2.Envelope) -> bool`。Task 2/3 依赖。

- [ ] **Step 1: 追加测试到 `tests/test_proto_codec.py`**

```python
from yuki.proto import yuki_pb2
from yuki.proto.codec import MAX_SUPPORTED_VERSION, version_supported


def test_max_supported_version_matches_version():
    from yuki.proto.codec import VERSION

    assert MAX_SUPPORTED_VERSION >= VERSION


def test_version_supported_accepts_current():
    env = yuki_pb2.Envelope(version=1)
    assert version_supported(env) is True


def test_version_supported_rejects_future_version():
    env = yuki_pb2.Envelope(version=MAX_SUPPORTED_VERSION + 1)
    assert version_supported(env) is False
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_proto_codec.py -v`
Expected: FAIL（`ImportError: cannot import name 'MAX_SUPPORTED_VERSION'`）。

- [ ] **Step 3: 修改 `src/yuki/proto/codec.py`**

```python
VERSION = 1
MAX_SUPPORTED_VERSION = 1


def version_supported(env: yuki_pb2.Envelope) -> bool:
    """当前进程能否解析该信封。超版本返回 False，调用方负责 drop + 告警。"""
    return int(env.version) <= MAX_SUPPORTED_VERSION
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_proto_codec.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/proto/codec.py tests/test_proto_codec.py
git commit -m "feat: add MAX_SUPPORTED_VERSION and version_supported helper"
```

---

### Task 2: BusHub 版本校验 + 未知 oneof + REGISTER 版本帧

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `tests/test_bus_faults.py`

**Interfaces:**
- Consumes: `version_supported`（Task 1）。
- Produces: `BusHub._router_loop` 在 request/response 前校验版本并 drop；`WhichOneof` 未知 kind 走 `else` 告警；REGISTER 解析读取可选版本帧，超版本拒绝注册。`BusNode._register_frames` 追加版本帧（Task 3 同步改，本任务先改 hub 侧兼容解析）。

- [ ] **Step 1: 追加失败测试到 `tests/test_bus_faults.py`**

```python
def test_hub_drops_future_version_envelope(make_bus):
    bus = make_bus(6181)
    time.sleep(0.1)

    # 模拟未来版本节点：直接向 hub ROUTER 端口发 request
    port = 6181
    ctx = zmq.Context.instance()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(f"tcp://127.0.0.1:{port + 2}")
    env = yuki_pb2.Envelope(version=99)
    env.request.service = "ghost"
    env.request.request_id = "future-req"
    env.request.payload.update({"x": 1})
    dealer.send_multipart([b"ghost", env.SerializeToString()])

    # hub 丢弃超版本 → 请求永远无响应
    assert dealer.poll(500) == 0
    dealer.close(linger=0)


def test_hub_rejects_register_with_future_version():
    port = 6182
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10)
    ctx = zmq.Context.instance()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(f"tcp://127.0.0.1:{port + 2}")
    try:
        time.sleep(0.1)
        # 未来版本 REGISTER：hub 拒绝 → 服务不注册
        dealer.send_multipart([b"REGISTER", b"incompat_svc", b"99"])
        time.sleep(0.2)
        with pytest.raises(BusError, match="service not found"):
            node.request("incompat_svc", {}, timeout_ms=1000)
    finally:
        dealer.close(linger=0)
        node.close()
        hub.close()


def test_hub_accepts_register_without_version_frame():
    port = 6183
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10)
    ctx = zmq.Context.instance()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(f"tcp://127.0.0.1:{port + 2}")
    try:
        time.sleep(0.1)
        # 旧节点：REGISTER 无版本帧 → 按 version 1 接受
        dealer.send_multipart([b"REGISTER", b"legacy_svc"])
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                assert node.request("legacy_svc", {}, timeout_ms=1000) == {}
                break
            except (BusError, BusTimeoutError):
                time.sleep(0.1)
        else:
            pytest.fail("legacy REGISTER without version frame was not accepted")
    finally:
        dealer.close(linger=0)
        node.close()
        hub.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_bus_faults.py -v -k "future_version or register_with_future_version or register_without_version"`
Expected: FAIL——当前 hub 不校验版本，未来版本 REGISTER 也会被注册，`incompat_svc` 请求会成功。

- [ ] **Step 3: 修改 `src/yuki/bus.py` 的 hub 侧**

- import 区新增：`version_supported`。
- `_router_loop` 的 REGISTER 分支改为读取可选版本帧：

```python
            if b"REGISTER" == frames[1]:  # auth-aware registration
                version = 1
                if self._auth_token:
                    if len(frames) not in (4, 5) or not _token_ok(self._auth_token, frames[2]):
                        logger.warning("dropping unauthorized REGISTER")
                        continue
                    service_frame = frames[3]
                    version_frame = frames[4] if len(frames) == 5 else None
                else:
                    if len(frames) not in (3, 4):
                        logger.warning("dropping malformed REGISTER frame count")
                        continue
                    service_frame = frames[2]
                    version_frame = frames[3] if len(frames) == 4 else None
                if version_frame is not None:
                    try:
                        version = int(version_frame.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        logger.warning("dropping REGISTER with malformed version frame")
                        continue
                if version > MAX_SUPPORTED_VERSION:
                    logger.warning(
                        "rejecting REGISTER from incompatible version",
                        service=service_frame.decode("utf-8", errors="replace"),
                        version=version,
                    )
                    continue
                try:
                    service = service_frame.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("dropping malformed REGISTER frame")
                    continue
                if not service:
                    logger.warning("dropping empty REGISTER service name")
                    continue
                self._service_map[service] = (sender, time.monotonic())
                continue
```

- `_router_loop` 的 request/response 路由前加版本校验 + 未知 oneof else：

```python
            raw = frames[-1] if self._auth_token else frames[2]
            f1 = frames[1]
            try:
                envelope = parse_envelope(raw)
            except DecodeError:
                logger.warning("dropping malformed envelope from %s", sender)
                continue
            if not version_supported(envelope):
                logger.warning(
                    "dropping unsupported envelope version",
                    version=envelope.version,
                )
                continue
            kind = envelope.WhichOneof("body")
            if kind == "request":
                ...
            elif kind == "response":
                self._router.send_multipart([f1, raw])
            else:
                logger.warning("unknown oneof kind in router", kind=kind)
```

注：`MAX_SUPPORTED_VERSION` 从 `yuki.proto.codec` 导入。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_bus_faults.py -v -k "future_version or register_with_future_version or register_without_version"`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/bus.py tests/test_bus_faults.py
git commit -m "feat: version-check envelopes and REGISTER frames in BusHub"
```

---

### Task 3: BusNode 版本校验 + 未知 oneof + REGISTER 版本帧

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `tests/test_bus_faults.py`

**Interfaces:**
- Consumes: `version_supported`（Task 1）。
- Produces: `BusNode._dealer_loop`/`_run_sub` 校验版本并 drop、未知 oneof 落 `else` 告警；`_register_frames` 追加 `str(VERSION)` 版本帧。

- [ ] **Step 1: 修改 `_register_frames` 追加版本帧**

在 `src/yuki/bus.py`：

```python
    def _register_frames(self, service: str) -> list:
        frames = [b"REGISTER"]
        if self._auth_token:
            frames.append(self._auth_token.encode("utf-8"))
        frames.append(service.encode())
        frames.append(str(VERSION).encode())
        return frames
```

- [ ] **Step 2: 修改 `_dealer_loop`（request 处理）**

解析后、`WhichOneof` 前加版本校验；`if envelope.WhichOneof("body") != "request":` 改为显式分支 + else 告警：

```python
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    envelope = parse_envelope(raw)
                except DecodeError:
                    continue
                if not version_supported(envelope):
                    logger.warning("dropping unsupported request version", version=envelope.version)
                    continue
                if envelope.trace_id:
                    bind_trace_id(envelope.trace_id)
                kind = envelope.WhichOneof("body")
                if kind == "request":
                    with self._services_lock:
                        handler = self._services.get(envelope.request.service)
                    if handler is None:
                        reply = build_response_error(
                            envelope.request.request_id, "service not found",
                            trace_id=envelope.trace_id,
                        )
                    else:
                        try:
                            result = handler(request_payload(envelope))
                            reply = build_response_result(
                                envelope.request.request_id, result,
                                trace_id=envelope.trace_id,
                            )
                        except Exception:
                            logger.error("responder handler failed", service=envelope.request.service)
                            self._bump_error()
                            reply = build_response_error(
                                envelope.request.request_id, "handler error",
                                trace_id=envelope.trace_id,
                            )
                    self._dealer.send_multipart([client_id, reply.SerializeToString()])
                else:
                    logger.warning("unknown oneof kind in dealer request path", kind=kind)
                if envelope.trace_id:
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    envelope = parse_envelope(frames[0])
                except DecodeError:
                    continue
                if not version_supported(envelope):
                    logger.warning("dropping unsupported response version", version=envelope.version)
                    continue
                kind = envelope.WhichOneof("body")
                if kind != "response":
                    logger.warning("unknown oneof kind in dealer response path", kind=kind)
                    continue
                rid = envelope.response.request_id
                ...
```

- [ ] **Step 3: 修改 `_run_sub`（事件处理）**

```python
            try:
                envelope = parse_envelope(raw_payload)
            except DecodeError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            if not version_supported(envelope):
                logger.warning("dropping unsupported event version", topic=topic, version=envelope.version)
                continue
            kind = envelope.WhichOneof("body")
            if kind != "event":
                logger.warning("unknown oneof kind in sub path", topic=topic, kind=kind)
                continue
            payload = event_payload(envelope)
```

- [ ] **Step 4: 追加/更新测试到 `tests/test_bus_faults.py`**

```python
def test_node_registers_with_version_frame(make_bus):
    # 确保新版 REGISTER 帧（带版本）在真实 hub 上可正常注册
    bus = make_bus(6184)
    bus.respond("svc", lambda p: {"echo": p.get("msg")})
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            assert bus.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}
            return
        except (BusError, BusTimeoutError):
            time.sleep(0.1)
    pytest.fail("versioned REGISTER was not accepted by hub")


def test_hub_rejects_future_version_envelope():
    # 直接对 hub 路由端口发未来版本 request，且 hub 已注册该服务 → 仍应被丢
    port = 6185
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10)
    node.respond("svc", lambda p: {"echo": 1})
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            assert node.request("svc", {}, timeout_ms=1000) == {"echo": 1}
            break
        except (BusError, BusTimeoutError):
            time.sleep(0.1)

    ctx = zmq.Context.instance()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(f"tcp://127.0.0.1:{port + 2}")
    env = yuki_pb2.Envelope(version=MAX_SUPPORTED_VERSION + 1)
    env.request.service = "svc"
    env.request.request_id = "future"
    env.request.payload.update({})
    dealer.send_multipart([b"svc", env.SerializeToString()])
    assert dealer.poll(500) == 0  # 未来版本被丢弃，无响应
    dealer.close(linger=0)
    node.close()
    hub.close()
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_bus_faults.py tests/test_bus.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/bus.py tests/test_bus_faults.py
git commit -m "feat: version-check envelopes in BusNode and versioned REGISTER frames"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 3 三目标全覆盖——解析后版本检查（Task 2/3）、未知 oneof `else` 告警（Task 2/3）、REGISTER 版本帧（Task 2 hub 拒绝 + Task 3 node 发送）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `MAX_SUPPORTED_VERSION`/`version_supported` 在 Task 1 定义，Task 2/3 导入同名使用；REGISTER 版本帧布局 `[REGISTER, token?, service, version?]` 在 Task 2 hub 解析与 Task 3 node 发送一致。
- **向后兼容：** 无版本帧的 REGISTER 仍按 version 1 接受（Task 2 测试覆盖）；`parse_envelope` 不抛版本错误。
