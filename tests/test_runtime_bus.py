import threading
import time

import pytest

from yuki.bus import BusError, BusTimeoutError
from yuki.runtime_bus import LocalRuntimeBus, LocalServiceCycleError, LocalServiceError


def test_event_handlers_are_ordered_and_isolated():
    bus = LocalRuntimeBus(subscriber_queue_size=8)
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_values = []

    def slow(topic, payload):
        del topic, payload
        slow_started.set()
        release_slow.wait(1.0)

    def fast(topic, payload):
        del topic
        fast_values.append(payload["n"])

    try:
        bus.subscribe("event/", slow)
        bus.subscribe("event/", fast)
        bus.publish("event/a", {"n": 1})
        bus.publish("event/a", {"n": 2})
        assert slow_started.wait(1.0)
        deadline = time.monotonic() + 1.0
        while fast_values != [1, 2] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fast_values == [1, 2]
    finally:
        release_slow.set()
        bus.close()


def test_slow_subscriber_only_drops_its_own_events():
    bus = LocalRuntimeBus(subscriber_queue_size=1)
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_count = 0
    fast_done = threading.Event()

    def slow(topic, payload):
        del topic, payload
        slow_started.set()
        release_slow.wait(1.0)

    def fast(topic, payload):
        nonlocal fast_count
        del topic, payload
        fast_count += 1
        if fast_count == 3:
            fast_done.set()

    try:
        bus.subscribe("event/", slow)
        bus.subscribe("event/", fast)
        bus.publish("event/x", {"n": 0})
        assert slow_started.wait(1.0)
        for value in range(1, 4):
            bus.publish("event/x", {"n": value})
            time.sleep(0.02)
        assert fast_done.wait(1.0)
        assert bus.dropped_count >= 1
    finally:
        release_slow.set()
        bus.close()


def test_pause_drops_events_until_resume():
    bus = LocalRuntimeBus()
    received = []
    try:
        bus.subscribe("event/", lambda topic, payload: received.append(payload))
        bus.pause_subscriptions()
        bus.publish("event/x", {"n": 1})
        bus.resume_subscriptions()
        bus.publish("event/x", {"n": 2})
        deadline = time.monotonic() + 1.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received == [{"n": 2}]
    finally:
        bus.close()


def test_local_service_cycle_is_rejected_and_uses_bus_error_hierarchy():
    bus = LocalRuntimeBus()
    bus.respond("a", lambda payload: bus.request("b", payload))
    bus.respond("b", lambda payload: bus.request("a", payload))
    try:
        with pytest.raises(LocalServiceCycleError):
            bus.request("a", {})
        with pytest.raises(BusError):
            bus.request("missing", {})
        assert issubclass(LocalServiceError, BusError)
    finally:
        bus.close()


def test_local_service_deadline_is_reported_after_handler_returns():
    bus = LocalRuntimeBus()
    bus.respond("slow", lambda payload: (time.sleep(0.03), {"ok": True})[1])
    try:
        with pytest.raises(BusTimeoutError):
            bus.request("slow", {}, timeout_ms=5)
        assert bus.services.timeout_count == 1
    finally:
        bus.close()


def test_handler_exception_is_wrapped_and_health_counts_it():
    bus = LocalRuntimeBus()

    def fail(payload):
        del payload
        raise ValueError("secret detail")

    bus.respond("fail", fail)
    try:
        with pytest.raises(LocalServiceError, match="handler error"):
            bus.request("fail", {})
        assert bus.health()["error_count"] == 1
    finally:
        bus.close()
