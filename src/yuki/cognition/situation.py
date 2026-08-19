import time
from typing import Callable


def scroll_band(scroll_percent: float | None) -> str:
    if scroll_percent is None:
        return "unknown"
    try:
        percent = min(max(float(scroll_percent), 0.0), 100.0)
    except (TypeError, ValueError):
        return "unknown"
    idx = min(int(percent // 25), 3)
    return f"{idx * 25}-{idx * 25 + 25}"


def source_id_for(observation: dict) -> str:
    return observation.get("source_id") or observation.get("url") or observation.get("title") or "unknown"


def cache_key_for(observation: dict) -> str:
    source_id = source_id_for(observation)
    scroll_percent = observation.get("scroll_percent")
    if scroll_percent is None:
        return source_id
    return f"{source_id}|{scroll_band(scroll_percent)}"


def deep_cache_key_for(observation: dict) -> str:
    return source_id_for(observation)


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def frame_id_for(observation: dict, frame: dict) -> int | None:
    return _int_or_none(frame.get("frame_id", observation.get("frame_id")))


def build_situation_update(
    observation: dict,
    frame: dict,
    context: dict,
    *,
    layer: str = "fast",
    confidence: float | None = None,
    sensitive: bool = False,
    reason: str | None = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    source_id = source_id_for(observation)
    scroll_percent = observation.get("scroll_percent")
    frame_id = frame_id_for(observation, frame)
    if frame_id is None:
        raise ValueError("situation update requires frame_id")
    frame_ts = frame.get("ts", observation.get("frame_ts", 0.0))

    if layer not in ("fast", "deep"):
        layer = "fast"
    if confidence is None:
        confidence = 0.0 if sensitive or context.get("degraded", False) else 0.6
    try:
        confidence = min(max(float(confidence), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0

    payload = {
        "situation_id": f"frame:{frame_id}",
        "source_id": source_id,
        "source_app": observation.get("app", ""),
        "source_title": observation.get("title", ""),
        "scroll_band": scroll_band(scroll_percent),
        "observation_reason": observation.get("reason", "unknown"),
        "observation_ts": observation.get("ts", 0.0),
        "frame_ts": frame_ts,
        "frame_width": frame.get("width", observation.get("frame_width", 0)),
        "frame_height": frame.get("height", observation.get("frame_height", 0)),
        "cache_key": cache_key_for(observation),
        "layer": layer,
        "confidence": confidence,
        "topic": "" if sensitive else context.get("topic", ""),
        "summary": "" if sensitive else context.get("summary", ""),
        "content_type": "unknown" if sensitive else context.get("content_type", "unknown"),
        "key_points": [] if sensitive else context.get("key_points", []),
        "sensitive": bool(sensitive),
        "degraded": bool(sensitive or context.get("degraded", False)),
        "reason": reason if reason is not None else context.get("reason", ""),
        "ts": clock(),
        "frame_id": frame_id,
    }
    if scroll_percent is not None:
        payload["scroll_percent"] = scroll_percent
    return payload
