import pytest

from yuki.bus import BusError, BusTimeoutError
from yuki.cognition.frame_client import FrameClient


def test_get_latest_returns_frame():
    class FakeBus:
        def request(self, service, payload, timeout_ms=2000):
            assert service == "frame"
            return {"png": "AAA", "width": 100, "height": 50, "ts": 1.0, "sensitive": False}

    client = FrameClient(FakeBus())
    assert client.get_latest()["width"] == 100


def test_get_latest_degrades_on_timeout():
    class FakeBus:
        def request(self, service, payload, timeout_ms=2000):
            raise BusTimeoutError("timeout")

    client = FrameClient(FakeBus())
    assert client.get_latest() == {}
