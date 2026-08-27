import base64
import time

import numpy as np

from yuki.bus_bridge import BusCompatibilityBridge, WireCodec, WireRuntimeBusAdapter
from yuki.runtime_bus import LocalRuntimeBus
from yuki.topics import Topics


class FakeRemoteBus:
    def __init__(self):
        self.services = {}
        self.events = []
        self.requests = []

    @property
    def error_count(self):
        return 0

    def respond(self, service, handler, *, lane="work"):
        self.services[service] = handler

    def publish(self, topic, payload, *, trace_id=None):
        self.events.append((topic, payload))

    def request(self, service, payload, timeout_ms=2000):
        self.requests.append((service, payload, timeout_ms))
        return self.services[service](payload)

    def subscribe(self, prefix, handler):
        self.subscription = (prefix, handler)

    def pause_subscriptions(self):
        pass

    def resume_subscriptions(self):
        pass

    def bus_health(self):
        return {"healthy": True}

    def close(self):
        pass


def test_wire_codec_converts_native_audio_at_boundary():
    codec = WireCodec()
    samples = np.asarray([0.5, -0.5], dtype=np.float32)
    encoded = codec.encode_event(Topics.MIC, {"samples": samples, "sample_rate": 16000})
    assert "samples" not in encoded
    assert base64.b64decode(encoded["pcm"]) == samples.tobytes()
    decoded = codec.decode_event(Topics.MIC, encoded)
    assert decoded["samples"].tolist() == samples.tolist()
    assert decoded["samples"].flags.writeable is False


def test_wire_adapter_round_trips_frame_bytes():
    remote = FakeRemoteBus()
    remote.respond("frame", lambda payload: {"png": base64.b64encode(b"png").decode()})
    adapter = WireRuntimeBusAdapter(remote)
    assert adapter.request("frame", {})["png"] == b"png"


def test_bridge_proxies_services_and_mirrors_events():
    local = LocalRuntimeBus()
    remote = FakeRemoteBus()
    local.respond("echo", lambda payload: {"value": payload["value"]})
    bridge = BusCompatibilityBridge(local, remote, mirror_topic_prefixes=["event/"])
    bridge.start()
    try:
        assert remote.services["echo"]({"value": 3}) == {"value": 3}
        local.publish("event/test", {"ok": True})
        deadline = time.monotonic() + 1.0
        while not remote.events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert remote.events == [("event/test", {"ok": True})]
    finally:
        bridge.close()
        local.close()
