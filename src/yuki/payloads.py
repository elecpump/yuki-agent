from typing import NotRequired, TypedDict


class AwakePayload(TypedDict):
    source: str
    ts: float
    confidence: NotRequired[float]


class ReplyPayload(TypedDict):
    text: str
    ts: float


class FocusChangedPayload(TypedDict):
    app: str
    url: str
    title: str


class SituationUpdatePayload(TypedDict):
    source_id: str
    scroll_band: str
    topic: str
    summary: str
    content_type: str
    key_points: list[str]
    sensitive: bool
    degraded: bool
    reason: str
    ts: float


class UserUtterancePayload(TypedDict):
    text: str
    duration_s: float
    ts: float


class MicPayload(TypedDict):
    pcm: str
    sample_rate: int
    ts: float


class HeartbeatPayload(TypedDict):
    process: str
    ts: float
    healthy: bool
    components: dict[str, dict]


class FrameResult(TypedDict):
    png: str
    width: int
    height: int
    ts: float
    sensitive: bool


class HealthResult(TypedDict):
    process: str
    pid: int
    uptime_s: float
    error_count: int
    healthy: bool
    components: dict[str, dict]
