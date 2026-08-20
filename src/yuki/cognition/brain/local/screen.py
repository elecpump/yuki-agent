import hashlib

from yuki.cognition.pipeline import decode_png_b64
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.local.screen")


class VisionScreenAdapter:
    def __init__(self, frame_client, vlm, *, timeout_ms: int = 1200) -> None:
        self._frame_client = frame_client
        self._vlm = vlm
        self._timeout_ms = timeout_ms

    def inspect(self, question: str) -> dict:
        del self._timeout_ms
        frame = self._frame_client.get_latest()
        if not frame or not frame.get("png"):
            return self._degraded("no_frame")
        image = decode_png_b64(frame["png"])
        if image is None:
            return self._degraded("decode_failed")
        frame_id = frame.get("frame_id", "unknown")
        question_hash = hashlib.sha256((question or "").encode("utf-8")).hexdigest()
        cache_key = f"vision_route:{frame_id}:{question_hash}"
        try:
            if hasattr(self._vlm, "understand_for_question"):
                result = self._vlm.understand_for_question(image, question, cache_key=cache_key)
            else:
                result = self._vlm.understand(image, cache_key=cache_key)
        except Exception:
            logger.warning("vision route inspection failed", exc_info=True)
            return self._degraded("inference_failed")
        if not isinstance(result, dict):
            return self._degraded("invalid_result")
        data = dict(result)
        data["can_answer"] = bool(data.get("can_answer", False))
        data.setdefault("topic", "")
        data.setdefault("summary", "")
        data.setdefault("content_type", "unknown")
        data.setdefault("key_points", [])
        data.setdefault("frame_id", frame_id)
        return data

    def _degraded(self, reason: str) -> dict:
        return {
            "topic": "",
            "summary": "",
            "content_type": "unknown",
            "key_points": [],
            "can_answer": False,
            "degraded": True,
            "reason": reason,
        }
