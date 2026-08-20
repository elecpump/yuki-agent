from yuki.bus import BusError, BusTimeoutError
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.frame_client")


class FrameClient:
    """frame REQ/REP 客户端：拉取采集层最新帧，失败降级为空 dict。"""

    def __init__(self, bus, timeout_ms: int = 2000) -> None:
        self._bus = bus
        self._timeout_ms = timeout_ms

    def _request(self, payload: dict) -> dict:
        try:
            return self._bus.request("frame", payload, timeout_ms=self._timeout_ms)
        except (BusError, BusTimeoutError):
            logger.warning("frame request failed, degrading to empty")
            return {}

    def get_latest(self) -> dict:
        return self._request({})
