import json
from contextlib import nullcontext
from dataclasses import dataclass
from enum import StrEnum

from yuki.cognition.call_tracker import CallTracker
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.local.router")

CRISIS_KEYWORDS = ("自杀", "自伤", "不想活", "想死", "活着没意思", "想结束生命", "割腕")
EXPLICIT_PREFERENCE_MARKERS = (
    "我喜欢",
    "我不喜欢",
    "以后请",
    "以后不要",
    "请记住",
    "别再",
    "不要再",
    "说反了",
    "简短一点",
    "更简短",
    "温柔一点",
)


class GateRoute(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class RouterDecision:
    route: GateRoute
    confidence: float
    reason: str = ""

    @classmethod
    def cloud(cls, reason: str = "fallback") -> "RouterDecision":
        return cls(GateRoute.CLOUD, 0.0, reason=reason)


class LocalRouter:
    """L0 gate: classify an utterance as local or cloud after deterministic guards."""

    def __init__(
        self,
        model,
        *,
        threshold: float = 0.7,
        retry: int = 1,
        prompt_max_tokens: int = 1200,
        timeout_ms: int = 150,
        model_registry: CallTracker | None = None,
        model_name: str = "local_chat",
    ) -> None:
        self._model = model
        self._model_registry = model_registry
        self._model_name = model_name
        self._threshold = threshold
        self._retry = retry
        self._prompt_max_tokens = prompt_max_tokens
        self._timeout_ms = timeout_ms

    def warmup(self) -> None:
        if hasattr(self._model, "warmup"):
            self._model.warmup()

    def route(self, text: str, *, snapshot=None, situation: dict | None = None) -> RouterDecision:
        if is_crisis(text):
            return RouterDecision(GateRoute.CLOUD, 1.0, reason="crisis")
        if is_explicit_preference(text):
            return RouterDecision(GateRoute.CLOUD, 1.0, reason="explicit_preference")
        messages = self._messages(text, snapshot=snapshot, situation=situation)
        raw = ""
        for attempt in range(max(0, self._retry) + 1):
            try:
                with self._model_call_tracker():
                    raw = self._model.generate(
                        messages,
                        max_new_tokens=60,
                        timeout_ms=self._timeout_ms,
                    )
                    return self._parse_and_validate(raw)
            except Exception:
                logger.warning("local router failed", attempt=attempt, raw=raw, exc_info=True)
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": "上一次输出无效。只输出一个严格 JSON 对象，不要解释。",
                    },
                ]
        return RouterDecision.cloud("router_failed")

    def _model_call_tracker(self):
        if self._model_registry is None:
            return nullcontext()
        return self._model_registry.track_call(self._model_name)

    def _messages(self, text: str, *, snapshot=None, situation: dict | None = None) -> list[dict]:
        recent = []
        if snapshot is not None:
            for turn in list(getattr(snapshot, "recent_turns", ()) or ())[:3]:
                recent.append(
                    {
                        "kind": turn.get("kind", "turn"),
                        "content": str(turn.get("content", ""))[:200],
                    }
                )
        payload = {
            "utterance": text,
            "recent_turns": recent,
            "situation": situation or getattr(snapshot, "situation", None) or {},
            "routes": [item.value for item in GateRoute],
        }
        user = json.dumps(payload, ensure_ascii=False)
        max_chars = int(self._prompt_max_tokens * 1.5)
        if len(user) > max_chars:
            user = user[:max_chars]
        return [
            {
                "role": "system",
                "content": (
                    "你是本地低延迟守门员。只输出严格 JSON，字段为 route、confidence。"
                    'route 只能取 "local" 或 "cloud"：local 表示简单对话/闲聊/情感回应，'
                    "本地模型即可自然回复；cloud 表示需要查信息、执行命令、多步推理、复杂问题，"
                    "以及任何需要长期记住的显式用户偏好或纠正。"
                ),
            },
            {"role": "user", "content": user},
        ]

    def _parse_and_validate(self, raw: str) -> RouterDecision:
        data = _parse_json_object(raw)
        route = GateRoute(str(data.get("route", "")))
        confidence = float(data.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence out of range")
        if confidence < self._threshold:
            return RouterDecision.cloud("low_confidence")
        return RouterDecision(route, confidence, reason="router")


def is_crisis(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in CRISIS_KEYWORDS)


def is_explicit_preference(text: str) -> bool:
    normalized = (text or "").strip()
    return any(marker in normalized for marker in EXPLICIT_PREFERENCE_MARKERS)


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("router output must be object")
    return data
