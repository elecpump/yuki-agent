import pytest

from yuki.cognition.situation import build_situation_update, deep_cache_key_for, scroll_band


def test_build_situation_update_includes_provenance():
    payload = build_situation_update(
        {
            "app": "chrome",
            "url": "https://x.com/a",
            "title": "A",
            "reason": "scroll_idle",
            "ts": 10.0,
            "scroll_percent": 30,
        },
        {
            "frame_id": 42,
            "width": 801,
            "height": 601,
            "ts": 9.5,
            "sensitive": False,
        },
        {
            "topic": "climate",
            "summary": "s",
            "content_type": "article",
            "key_points": ["k"],
        },
        clock=lambda: 12.0,
    )

    assert payload == {
        "situation_id": "frame:42",
        "source_id": "https://x.com/a",
        "source_app": "chrome",
        "source_title": "A",
        "scroll_band": "25-50",
        "scroll_percent": 30,
        "observation_reason": "scroll_idle",
        "observation_ts": 10.0,
        "frame_id": 42,
        "frame_ts": 9.5,
        "frame_width": 801,
        "frame_height": 601,
        "cache_key": "https://x.com/a|25-50",
        "layer": "fast",
        "confidence": 0.6,
        "topic": "climate",
        "summary": "s",
        "content_type": "article",
        "key_points": ["k"],
        "sensitive": False,
        "degraded": False,
        "reason": "",
        "ts": 12.0,
    }


def test_build_sensitive_situation_preserves_provenance():
    payload = build_situation_update(
        {"url": "https://x.com/a", "reason": "focus_changed", "ts": 10.0},
        {"frame_id": 7, "width": 800, "height": 600, "ts": 9.0, "sensitive": False},
        {},
        sensitive=True,
        reason="sensitive",
        clock=lambda: 12.0,
    )

    assert payload["situation_id"] == "frame:7"
    assert payload["frame_id"] == 7
    assert payload["observation_reason"] == "focus_changed"
    assert payload["topic"] == ""
    assert payload["sensitive"] is True
    assert payload["degraded"] is True
    assert payload["layer"] == "fast"
    assert payload["confidence"] == 0.0
    assert payload["reason"] == "sensitive"


def test_build_deep_situation_uses_layer_confidence_and_source_cache_key():
    payload = build_situation_update(
        {"url": "https://x.com/a", "scroll_percent": 80, "frame_id": 7},
        {"frame_id": 7, "width": 800, "height": 600, "ts": 9.0},
        {"topic": "climate"},
        layer="deep",
        confidence=0.85,
    )

    assert payload["layer"] == "deep"
    assert payload["confidence"] == 0.85
    assert payload["cache_key"] == "https://x.com/a|75-100"
    assert deep_cache_key_for(payload) == "https://x.com/a"


def test_build_situation_update_requires_frame_id():
    with pytest.raises(ValueError, match="frame_id"):
        build_situation_update(
            {"url": "https://x.com/a", "reason": "focus_changed", "ts": 10.0},
            {"width": 800, "height": 600, "ts": 9.0, "sensitive": False},
            {},
        )


def test_scroll_band_clamps_to_valid_range():
    assert scroll_band(100) == "75-100"
    assert scroll_band(130) == "75-100"
    assert scroll_band(-10) == "0-25"
    assert scroll_band("30") == "25-50"
