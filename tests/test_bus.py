import threading
import time

from yuki.bus import MessageBus


def _wait_sub(t=0.1):
    time.sleep(t)


def test_publish_subscribe_roundtrip():
    bus = MessageBus(base_port=6001, hwm=10)
    received = threading.Event()
    got = {}

    def on_event(topic, payload):
        got["topic"] = topic
        got["payload"] = payload
        received.set()

    bus.subscribe("event/", on_event)
    _wait_sub()
    bus.publish("event/awake", {"source": "hotkey"})
    assert received.wait(timeout=2.0)
    assert got["topic"] == "event/awake"
    assert got["payload"] == {"source": "hotkey"}
    bus.close()


def test_subscribe_filters_by_prefix():
    bus = MessageBus(base_port=6002, hwm=10)
    hits = []

    def on_awake(topic, payload):
        hits.append(payload)

    bus.subscribe("event/awake", on_awake)
    _wait_sub()
    bus.publish("event/reply", {"text": "hi"})
    bus.publish("event/awake", {"source": "hotkey"})
    deadline = time.time() + 2.0
    while not hits and time.time() < deadline:
        time.sleep(0.05)
    assert hits == [{"source": "hotkey"}]
    bus.close()
