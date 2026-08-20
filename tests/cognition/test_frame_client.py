from yuki.bus import BusTimeoutError
from yuki.cognition.frame_client import FrameClient

from tests.fakes import FakeBus


def test_get_latest_returns_frame():
    bus = FakeBus()
    bus.respond(
        "frame",
        lambda payload: {
            "frame_id": 1,
            "png": "AAA",
            "width": 100,
            "height": 50,
            "ts": 1.0,
            "sensitive": False,
        },
    )
    client = FrameClient(bus)
    assert client.get_latest()["width"] == 100


def test_get_latest_degrades_on_timeout():
    bus = FakeBus()

    def handler(payload):
        raise BusTimeoutError("timeout")

    bus.respond("frame", handler)
    client = FrameClient(bus)
    assert client.get_latest() == {}
