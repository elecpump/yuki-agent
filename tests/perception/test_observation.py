from yuki.perception.observation import StableContentObservation
from yuki.topics import Topics

from tests.fakes import FakeBus


def test_focus_change_waits_for_stored_frame_before_content_ready():
    bus = FakeBus()
    obs = StableContentObservation(bus, clock=lambda: 123.0)

    obs.on_focus_changed({"app": "chrome", "url": "https://example.com/a", "title": "A"})
    assert bus.published == []

    obs.on_frame_stored(
        {"frame_id": 7, "ts": 99.0, "width": 800, "height": 600, "sensitive": False}
    )

    assert bus.published == [
        (
            Topics.CONTENT_READY,
            {
                "app": "chrome",
                "url": "https://example.com/a",
                "title": "A",
                "reason": "focus_changed",
                "frame_id": 7,
                "ts": 123.0,
                "frame_ts": 99.0,
                "frame_width": 800,
                "frame_height": 600,
                "sensitive": False,
            },
        )
    ]


def test_scroll_idle_uses_last_focus_when_next_frame_is_stored():
    bus = FakeBus()
    obs = StableContentObservation(bus, clock=lambda: 200.0)
    obs.on_focus_changed({"app": "chrome", "url": "https://example.com/a", "title": "A"})
    obs.on_frame_stored(
        {"frame_id": 1, "ts": 1.0, "width": 800, "height": 600, "sensitive": False}
    )
    bus.published = []

    obs.on_scroll_activity()
    assert bus.published == []

    obs.on_frame_stored(
        {"frame_id": 2, "ts": 2.0, "width": 800, "height": 600, "sensitive": False}
    )

    assert bus.published[0][0] == Topics.CONTENT_READY
    payload = bus.published[0][1]
    assert payload["reason"] == "scroll_idle"
    assert payload["frame_id"] == 2
    assert payload["url"] == "https://example.com/a"
    assert payload["frame_ts"] == 2.0


def test_focus_pending_reason_is_not_overwritten_by_scroll_before_frame():
    bus = FakeBus()
    obs = StableContentObservation(bus, clock=lambda: 300.0)

    obs.on_focus_changed({"app": "chrome", "url": "https://example.com/a", "title": "A"})
    obs.on_scroll_activity()

    obs.on_frame_stored(
        {"frame_id": 1, "ts": 1.0, "width": 800, "height": 600, "sensitive": False}
    )

    assert bus.published[0][1]["reason"] == "focus_changed"
    assert bus.published[0][1]["frame_id"] == 1

    bus.published = []
    obs.on_frame_stored(
        {"frame_id": 2, "ts": 2.0, "width": 800, "height": 600, "sensitive": False}
    )

    assert bus.published[0][1]["reason"] == "scroll_idle"
    assert bus.published[0][1]["frame_id"] == 2


def test_frame_without_pending_observation_is_silent():
    bus = FakeBus()
    obs = StableContentObservation(bus)

    obs.on_frame_stored({"ts": 1.0, "width": 800, "height": 600, "sensitive": False})

    assert bus.published == []


def test_pending_observation_requires_identified_frame():
    bus = FakeBus()
    obs = StableContentObservation(bus)

    obs.on_focus_changed({"app": "chrome", "url": "https://example.com/a", "title": "A"})
    obs.on_frame_stored({"ts": 1.0, "width": 800, "height": 600, "sensitive": False})

    assert bus.published == []
