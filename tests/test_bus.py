import threading
import time

import pytest

from yuki.bus import MessageBus


def _wait_sub(t=0.05):
    time.sleep(t)  # ZMQ PUB/SUB slow-joiner 保护


def test_publish_subscribe_roundtrip():
    bus = MessageBus(base_port=6001)
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


def test_subscribe_filters_by_prefix():
    bus = MessageBus(base_port=6002)
    hits = []

    def on_awake(topic, payload):
        hits.append(payload)

    bus.subscribe("event/awake", on_awake)
    _wait_sub()
    bus.publish("event/reply", {"text": "hi"})
    bus.publish("event/awake", {"source": "hotkey"})
    time.sleep(0.3)
    assert hits == [{"source": "hotkey"}]


def test_request_respond_roundtrip():
    bus = MessageBus(base_port=6003)

    def handler(payload):
        return {"echo": payload["msg"]}

    bus.respond("ping", handler)
    time.sleep(0.05)
    result = bus.request("ping", {"msg": "hello"})
    assert result == {"echo": "hello"}
