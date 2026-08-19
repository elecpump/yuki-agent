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
    hwnd: NotRequired[int]
    # Phase 4 UI Automation can provide this reliably; current capture may omit it.
    scroll_percent: NotRequired[float]


class ContentReadyPayload(TypedDict):
    app: str
    url: str
    title: str
    reason: str
    frame_id: int
    ts: float
    frame_ts: float
    frame_width: int
    frame_height: int
    sensitive: bool
    hwnd: NotRequired[int]
    scroll_percent: NotRequired[float]


class SituationUpdatePayload(TypedDict):
    situation_id: str
    source_id: str
    source_app: str
    source_title: str
    scroll_band: str
    observation_reason: str
    observation_ts: float
    frame_id: int
    frame_ts: float
    frame_width: int
    frame_height: int
    cache_key: str
    layer: str
    confidence: float
    topic: str
    summary: str
    content_type: str
    key_points: list[str]
    sensitive: bool
    degraded: bool
    reason: str
    ts: float
    scroll_percent: NotRequired[float]


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
    frame_id: NotRequired[int]
    png: str
    width: int
    height: int
    ts: float
    sensitive: bool
    hwnd: NotRequired[int]


class TextEvidencePayload(TypedDict):
    text_id: NotRequired[int]
    source: str
    text: str
    title: str
    url: str
    hwnd: NotRequired[int]
    frame_id: NotRequired[int]
    confidence: float
    ts: float
    sensitive: bool
    degraded: bool
    reason: str


class HealthResult(TypedDict):
    process: str
    pid: int
    uptime_s: float
    error_count: int
    healthy: bool
    components: dict[str, dict]


class MemoryWritePayload(TypedDict):
    memory_type: str
    content: str
    confidence: NotRequired[float]
    sensitivity: NotRequired[int]
    source: NotRequired[str]
    metadata: NotRequired[dict]


class MemoryQueryPayload(TypedDict):
    text: str
    type: NotRequired[str]
    top_k: NotRequired[int]
    min_sensitivity: NotRequired[int]


class MemoryListPayload(TypedDict):
    type: NotRequired[str]
    min_sensitivity: NotRequired[int]


class MemoryGetPayload(TypedDict):
    id: int


class MemoryDeletePayload(TypedDict):
    id: int


class MemoryStrengthenPayload(TypedDict):
    id: int


class MemoryResult(TypedDict):
    id: int
    memory_type: str
    content: str
    confidence: float
    sensitivity: int
    source: str
    metadata: dict
    created_at: float
    last_access: float
    access_count: int
    strengthened: bool
    score: NotRequired[float]


class MemoryWriteResult(TypedDict):
    id: int
