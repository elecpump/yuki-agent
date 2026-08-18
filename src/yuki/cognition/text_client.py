from yuki.bus import BusError, BusTimeoutError
from yuki.logger import get_logger

TEXT_SERVICE = "text"

logger = get_logger("yuki.cognition.text_client")


class TextClient:
    """text REQ/REP client: fetches gated text evidence from perception."""

    def __init__(self, bus, timeout_ms: int = 200) -> None:
        self._bus = bus
        self._timeout_ms = timeout_ms

    def get_for_observation(self, observation: dict, frame: dict | None = None) -> dict:
        payload = dict(observation or {})
        if frame:
            payload.setdefault("frame_id", frame.get("frame_id"))
            payload.setdefault("hwnd", frame.get("hwnd"))
            payload.setdefault("frame_ts", frame.get("ts"))
        try:
            return self._bus.request(TEXT_SERVICE, payload, timeout_ms=self._timeout_ms)
        except (BusError, BusTimeoutError, RuntimeError):
            logger.debug("text request failed, falling back", exc_info=True)
            return {}
