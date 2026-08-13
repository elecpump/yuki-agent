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
