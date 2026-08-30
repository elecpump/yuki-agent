import threading
import time

import pytest
import zmq

from yuki.bus import BusError, BusTimeoutError, BusHub, BusNode
from yuki.proto import yuki_pb2
from yuki.proto.codec import MAX_SUPPORTED_VERSION


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


def test_request_respond_roundtrip(make_bus):
    bus = make_bus(6110)
    bus.respond("ping", lambda payload: {"echo": payload["msg"]})
    time.sleep(0.1)
    assert bus.request("ping", {"msg": "hello"}, timeout_ms=1000) == {"echo": "hello"}


def test_embedded_hub_exposes_health_snapshot_for_app_aggregation():
    hub = BusHub(base_port=6109, hwm=10)
    try:
        snapshot = hub.health_snapshot()
    finally:
        hub.close()

    assert snapshot["healthy"] is True
    assert snapshot["components"]["proxy"]["ok"] is True


def test_request_unregistered_service_raises_bus_error(make_bus):
    bus = make_bus(6120)
    time.sleep(0.1)
    with pytest.raises(BusError, match="service not found"):
        bus.request("ghost", {}, timeout_ms=1000)


def test_request_times_out_when_handler_hangs(make_bus):
    bus = make_bus(6121)

    def slow(payload):
        time.sleep(1.0)
        return {"ok": 1}

    bus.respond("slow", slow)
    time.sleep(0.1)
    with pytest.raises(BusTimeoutError):
        bus.request("slow", {}, timeout_ms=200)


def test_request_raises_bus_error_on_failed_handler(make_bus):
    bus = make_bus(6130)

    def handler(payload):
        raise ValueError("boom")

    bus.respond("boom", handler)
    time.sleep(0.1)
    with pytest.raises(BusError, match="handler error"):
        bus.request("boom", {}, timeout_ms=1000)


def test_responder_handler_can_make_nested_request_on_same_node(make_bus):
    bus = make_bus(6122)
    bus.respond("inner", lambda payload: {"value": payload["value"] + 1})
    bus.respond(
        "outer",
        lambda payload: bus.request("inner", payload, timeout_ms=1000),
    )
    time.sleep(0.1)

    assert bus.request("outer", {"value": 1}, timeout_ms=1500) == {"value": 2}


def test_control_lane_remains_available_during_long_work_handler():
    port = 6123
    hub = BusHub(base_port=port, hwm=10)
    bus = BusNode(base_port=port, hwm=10, work_workers=1)
    started = threading.Event()
    release = threading.Event()

    def slow(payload):
        del payload
        started.set()
        release.wait(2.0)
        return {"ok": True}

    bus.respond("slow", slow, lane="work")
    bus.respond("health/test", lambda payload: {"healthy": True}, lane="control")
    time.sleep(0.1)
    caller = threading.Thread(
        target=lambda: bus.request("slow", {}, timeout_ms=2000),
        daemon=True,
    )
    caller.start()
    try:
        assert started.wait(1.0)
        assert bus.request("health/test", {}, timeout_ms=500) == {"healthy": True}
    finally:
        release.set()
        caller.join(timeout=2.0)
        bus.close()
        hub.close()


def test_full_responder_lane_returns_server_busy():
    port = 6124
    hub = BusHub(base_port=port, hwm=20)
    bus = BusNode(
        base_port=port,
        hwm=20,
        work_workers=1,
        responder_queue_size=1,
    )
    started = threading.Event()
    release = threading.Event()

    def slow(payload):
        started.set()
        release.wait(2.0)
        return {"call": payload["call"]}

    bus.respond("slow", slow)
    time.sleep(0.1)
    results = []

    def request(call):
        results.append(bus.request("slow", {"call": call}, timeout_ms=2000))

    first = threading.Thread(target=request, args=(1,), daemon=True)
    second = threading.Thread(target=request, args=(2,), daemon=True)
    first.start()
    try:
        assert started.wait(1.0)
        second.start()
        time.sleep(0.1)
        with pytest.raises(BusError, match="server busy"):
            bus.request("slow", {"call": 3}, timeout_ms=500)
    finally:
        release.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        bus.close()
        hub.close()
    assert sorted(item["call"] for item in results) == [1, 2]


def test_respond_loop_survives_handler_exception(make_bus):
    bus = make_bus(6140)

    def handler(payload):
        if payload.get("bad"):
            raise ValueError("bad payload")
        return {"echo": payload["msg"]}

    bus.respond("svc", handler)
    time.sleep(0.1)
    with pytest.raises(BusError, match="handler error"):
        bus.request("svc", {"bad": True}, timeout_ms=1000)
    assert bus.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}


def test_sub_thread_survives_handler_exception(make_bus):
    bus = make_bus(6150)
    received = threading.Event()
    got = []

    def on_event(topic, payload):
        if payload.get("bad"):
            raise RuntimeError("handler boom")
        got.append(payload)
        received.set()

    bus.subscribe("event/", on_event)
    time.sleep(0.1)
    bus.publish("event/a", {"bad": True})
    bus.publish("event/b", {"ok": 1})
    assert received.wait(timeout=2.0)
    assert got == [{"ok": 1}]


def test_multiple_handlers_same_prefix_all_called(make_bus):
    bus = make_bus(6160)
    calls = []

    def h1(topic, payload):
        calls.append("h1")

    def h2(topic, payload):
        calls.append("h2")

    bus.subscribe("event/", h1)
    bus.subscribe("event/", h2)
    time.sleep(0.1)
    bus.publish("event/x", {})
    deadline = time.time() + 2.0
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(calls) == ["h1", "h2"]


def test_overlapping_prefixes_both_dispatch(make_bus):
    bus = make_bus(6170)
    got = []

    def broad(topic, payload):
        got.append("broad")

    def narrow(topic, payload):
        got.append("narrow")

    bus.subscribe("event/", broad)
    bus.subscribe("event/awake", narrow)
    time.sleep(0.1)
    bus.publish("event/awake", {})
    deadline = time.time() + 2.0
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(got) == ["broad", "narrow"]


def test_services_reregister_after_hub_restart():
    port = 6180
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10, register_interval=0.2)
    node.respond("svc", lambda payload: {"echo": payload["msg"]})

    deadline = time.time() + 5.0
    registered = False
    while time.time() < deadline:
        try:
            if node.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}:
                registered = True
                break
        except BusError:
            time.sleep(0.1)
    assert registered, "initial registration did not take effect"

    hub.close()
    time.sleep(0.3)

    hub2 = BusHub(base_port=port, hwm=10)

    try:
        deadline = time.time() + 8.0
        last_error = None
        while time.time() < deadline:
            time.sleep(0.1)
            try:
                assert node.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}
                return
            except BusError as exc:
                last_error = exc
        pytest.fail(f"service not re-registered after hub restart: {last_error}")
    finally:
        hub2.close()
        node.close()


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


def test_hub_drops_future_version_envelope(make_bus):
    bus = make_bus(6181)
    time.sleep(0.1)

    ctx = zmq.Context.instance()
    dealer = ctx.socket(zmq.DEALER)
    dealer.connect("tcp://127.0.0.1:6183")
    try:
        env = yuki_pb2.Envelope(version=MAX_SUPPORTED_VERSION + 1)
        env.request.service = "ghost"
        env.request.request_id = "future-req"
        env.request.payload.update({"x": 1})
        dealer.send_multipart([b"ghost", env.SerializeToString()])

        assert dealer.poll(500) == 0
    finally:
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
        dealer.send_multipart([b"REGISTER", b"incompat_svc", b"99"])
        time.sleep(0.2)
        assert "incompat_svc" not in hub._service_map
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
        dealer.send_multipart([b"REGISTER", b"legacy_svc"])
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "legacy_svc" in hub._service_map:
                break
            time.sleep(0.1)
        else:
            pytest.fail("legacy REGISTER without version frame was not accepted")
    finally:
        dealer.close(linger=0)
        node.close()
        hub.close()


def test_node_registers_with_version_frame(make_bus):
    bus = make_bus(6184)
    bus.respond("svc", lambda p: {"echo": p.get("msg")})
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            assert bus.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}
            return
        except BusError:
            time.sleep(0.1)
    pytest.fail("versioned REGISTER was not accepted by hub")


def test_many_bus_create_close_cycles():
    # 回归：单进程内反复创建/关闭多个 MessageBus，曾触发 libzmq 4.3.5
    # Windows signaler 断言（signaler.cpp:345）。修复后应稳定通过。
    for i in range(25):
        hub = BusHub(base_port=6800 + i, hwm=10)
        node = BusNode(base_port=6800 + i, hwm=10)
        node.respond("ping", lambda p: {"echo": p["msg"]})
        time.sleep(0.01)
        node.request("ping", {"msg": "hi"}, timeout_ms=1000)
        hub.close()
        node.close()


def test_bus_wire_format_is_protobuf():
    hub = BusHub(base_port=6900, hwm=10)
    node = BusNode(base_port=6900, hwm=10)
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
    node.close()


def test_wire_frame_second_part_is_envelope():
    # 探测 ROUTER 直接扮演 hub：绑定 base_port+2 的 ROUTER 端口，
    # node 作为纯节点连接它。若同时创建 hub 实例，hub 会占用 6903，
    # probe 再绑定会 Address in use。
    node = BusNode(base_port=6901, hwm=10)
    ctx = zmq.Context.instance()
    probe = ctx.socket(zmq.ROUTER)
    probe.bind("tcp://127.0.0.1:6903")  # base_port+2

    node.respond("ping", lambda p: {"echo": p["msg"]})
    time.sleep(0.2)
    try:
        try:
            node.request("ping", {"msg": "hi"}, timeout_ms=1000)
        except BusTimeoutError:
            pass  # probe 只观察不回复，请求超时是预期行为

        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                frames = probe.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.ZMQError:
                time.sleep(0.05)
                continue
            for frame in frames:
                try:
                    parsed = yuki_pb2.Envelope.FromString(frame)
                except Exception:
                    continue
                if parsed.WhichOneof("body") == "request" and parsed.request.service == "ping":
                    assert parsed.request.request_id != ""
                    assert parsed.request.payload["msg"] == "hi"
                    return
        pytest.fail("did not observe a protobuf request envelope on the wire")
    finally:
        node.close()
        probe.close(linger=0)

def test_auth_token_authorized_request_and_wrong_token_rejected():
    port = 6911
    hub = BusHub(base_port=port, hwm=10, auth_token="secret")
    node = BusNode(base_port=port, hwm=10, auth_token="secret")
    bad = BusNode(base_port=port, hwm=10, auth_token="wrong")
    try:
        node.respond("svc", lambda p: {"echo": p["msg"]})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                assert node.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}
                break
            except BusError:
                time.sleep(0.1)
        else:
            pytest.fail("authorized request did not complete")

        with pytest.raises(BusError, match="unauthorized"):
            bad.request("svc", {"msg": "hi"}, timeout_ms=1000)
    finally:
        bad.close()
        node.close()
        hub.close()


def test_auth_token_events_roundtrip_and_foreign_events_ignored():
    port = 6912
    hub = BusHub(base_port=port, hwm=10, auth_token="secret")
    node = BusNode(base_port=port, hwm=10, auth_token="secret")
    bad = BusNode(base_port=port, hwm=10, auth_token="wrong")
    received = []

    def on_event(topic, payload):
        received.append(payload)

    try:
        node.subscribe("event/", on_event)
        time.sleep(0.1)
        bad.publish("event/foreign", {"bad": True})
        time.sleep(0.1)
        assert received == []
        node.publish("event/ok", {"ok": True})
        deadline = time.time() + 2.0
        while not received and time.time() < deadline:
            time.sleep(0.05)
        assert received == [{"ok": True}]
    finally:
        bad.close()
        node.close()
        hub.close()


def test_bus_hub_stays_healthy_while_idle_without_traffic():
    """Regression: an idle-but-alive proxy loop must not report unhealthy.

    BusHub liveness is defined by the proxy thread's heartbeat, not by
    message forwarding volume. An idle system (no publishers, no gateway)
    previously flipped BUS_HEALTH_SERVICE to healthy:false ~5s after start,
    which made the supervisor restart yuki in a loop until the restart
    window cap was hit.
    """
    import yuki.bus as bus_mod

    port = 6913
    hub = bus_mod.BusHub(base_port=port, hwm=10)
    try:
        hub._last_proxy_forwarded = time.monotonic() - bus_mod.PROXY_STALE_S - 1.0
        # Proxy loop keeps beating; only forwarding is stale.
        hub._last_proxy_activity = time.monotonic()
        health = hub._collect_health()
        assert health["healthy"] is True
        assert health["components"]["proxy"]["ok"] is True
        assert "last_forwarded_s" in health["components"]["proxy"]
    finally:
        hub.close()


def test_bus_hub_health_reflects_proxy_loop_liveness():
    import yuki.bus as bus_mod

    port = 6914
    hub = bus_mod.BusHub(base_port=port, hwm=10)
    node = bus_mod.BusNode(base_port=port, hwm=10)
    try:
        received = threading.Event()
        node.subscribe("event/", lambda t, p: received.set())
        time.sleep(0.1)
        # 1. Fresh heartbeat, stale forwarding -> alive (idle is not dead).
        hub._last_proxy_forwarded = time.monotonic() - bus_mod.PROXY_STALE_S - 1.0
        hub._last_proxy_activity = time.monotonic()
        assert hub._collect_health()["healthy"] is True
        # 2. Stale heartbeat -> proxy loop is actually dead -> unhealthy.
        hub._last_proxy_activity = time.monotonic() - bus_mod.PROXY_STALE_S - 1.0
        assert hub._collect_health()["healthy"] is False
        # 3. Traffic resumes and loop heartbeats -> healthy again.
        hub._last_proxy_activity = time.monotonic()
        node.publish("event/x", {"x": 1})
        assert received.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if hub._collect_health()["healthy"]:
                break
            time.sleep(0.02)
        assert hub._collect_health()["healthy"] is True
    finally:
        node.close()
        hub.close()
