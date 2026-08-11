import threading
import time

import pytest

from yuki.bus import BusError, BusTimeoutError, MessageBus


@pytest.fixture()
def make_bus():
    buses = []

    def _make(port, role="hub", **kwargs):
        bus = MessageBus(base_port=port, role=role, hwm=10, **kwargs)
        buses.append(bus)
        return bus

    yield _make
    for bus in buses:
        bus.close()


def test_request_respond_roundtrip(make_bus):
    bus = make_bus(6110)
    bus.respond("ping", lambda payload: {"echo": payload["msg"]})
    time.sleep(0.1)
    assert bus.request("ping", {"msg": "hello"}, timeout_ms=1000) == {"echo": "hello"}


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


def test_services_reregister_after_hub_restart(make_bus):
    port = 6180
    hub = make_bus(port, role="hub")
    node = make_bus(port, role="node", register_interval=0.2)
    node.respond("svc", lambda payload: {"echo": payload["msg"]})

    deadline = time.time() + 5.0
    registered = False
    while time.time() < deadline:
        try:
            if node.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}:
                registered = True
                break
        except (BusError, BusTimeoutError):
            time.sleep(0.1)
    assert registered, "initial registration did not take effect"

    hub.close()
    time.sleep(0.3)

    hub2 = make_bus(port, role="hub")

    deadline = time.time() + 8.0
    last_error = None
    while time.time() < deadline:
        time.sleep(0.1)
        try:
            assert node.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}
            return
        except (BusError, BusTimeoutError) as exc:
            last_error = exc
    pytest.fail(f"service not re-registered after hub restart: {last_error}")
