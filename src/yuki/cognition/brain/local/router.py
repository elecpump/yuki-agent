import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.functions.registry import FunctionRegistry
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.local.router")

CRISIS_KEYWORDS = ("自杀", "自伤", "不想活", "想死", "活着没意思", "想结束生命", "割腕")


class LocalRoute(StrEnum):
    CHAT_LOCAL = "chat_local"
    TOOL_LOCAL = "tool_local"
    VISION = "vision"
    CLOUD = "cloud"


@dataclass(frozen=True)
class RouterDecision:
    route: LocalRoute
    confidence: float
    intent: Intent = Intent.UNKNOWN
    emotion: Emotion = Emotion.NEUTRAL
    tool_call: dict | None = None
    trusted_metadata: bool = False
    reason: str = ""

    @classmethod
    def cloud(cls, reason: str = "fallback") -> "RouterDecision":
        return cls(LocalRoute.CLOUD, 0.0, reason=reason)


class LocalRouter:
    def __init__(
        self,
        model,
        *,
        registry: FunctionRegistry | None = None,
        threshold: float = 0.7,
        retry: int = 1,
        prompt_max_tokens: int = 1200,
        timeout_ms: int = 150,
        local_tool_allowlist: list[str] | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._threshold = threshold
        self._retry = retry
        self._prompt_max_tokens = prompt_max_tokens
        self._timeout_ms = timeout_ms
        self._allowlist = set(local_tool_allowlist or [])

    def warmup(self) -> None:
        if hasattr(self._model, "warmup"):
            self._model.warmup()

    def route(self, text: str, *, snapshot=None, situation: dict | None = None) -> RouterDecision:
        if is_crisis(text):
            return RouterDecision(
                LocalRoute.CLOUD,
                1.0,
                intent=Intent.SAFETY,
                emotion=Emotion.SADNESS,
                trusted_metadata=False,
                reason="crisis",
            )
        messages = self._messages(text, snapshot=snapshot, situation=situation)
        raw = ""
        for attempt in range(max(0, self._retry) + 1):
            try:
                raw = self._model.generate(messages, max_new_tokens=180, timeout_ms=self._timeout_ms)
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

    def tool_summaries(self) -> list[dict]:
        if self._registry is None or not self._allowlist:
            return []
        schemas_by_name = {
            item.get("function", {}).get("name"): item.get("function", {})
            for item in self._registry.tool_schemas()
        }
        summaries = []
        for name in sorted(self._allowlist):
            schema = schemas_by_name.get(name)
            if not schema:
                continue
            params = schema.get("parameters") or {}
            properties = params.get("properties") or {}
            summaries.append({
                "name": name,
                "description": schema.get("description", ""),
                "params": sorted(properties),
            })
        return summaries

    def _messages(self, text: str, *, snapshot=None, situation: dict | None = None) -> list[dict]:
        recent = []
        if snapshot is not None:
            for turn in list(getattr(snapshot, "recent_turns", ()) or ())[:5]:
                recent.append({
                    "kind": turn.get("kind", "turn"),
                    "content": str(turn.get("content", ""))[:300],
                })
        payload = {
            "utterance": text,
            "recent_turns": recent,
            "situation": situation or getattr(snapshot, "situation", None) or {},
            "intents": [item.value for item in Intent],
            "emotions": [item.value for item in Emotion],
            "routes": [item.value for item in LocalRoute],
            "allowed_tools": self.tool_summaries(),
        }
        user = json.dumps(payload, ensure_ascii=False)
        max_chars = int(self._prompt_max_tokens * 1.5)
        if len(user) > max_chars:
            user = user[:max_chars]
        return [
            {
                "role": "system",
                "content": (
                    "你是本地低延迟路由器。只输出严格 JSON，字段为 route、confidence、"
                    "intent、emotion、tool_call。tool_local 只能使用 allowed_tools 中的工具。"
                ),
            },
            {"role": "user", "content": user},
        ]

    def _parse_and_validate(self, raw: str) -> RouterDecision:
        data = _parse_json_object(raw)
        route = LocalRoute(str(data.get("route", "")))
        confidence = float(data.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence out of range")
        if confidence < self._threshold:
            return RouterDecision.cloud("low_confidence")
        intent = Intent(str(data.get("intent", Intent.UNKNOWN.value)))
        emotion = Emotion(str(data.get("emotion", Emotion.NEUTRAL.value)))
        tool_call = data.get("tool_call", data.get("function_call"))
        if route == LocalRoute.TOOL_LOCAL:
            self._validate_tool_call(tool_call)
        elif tool_call is not None:
            self._validate_tool_call(tool_call)
        return RouterDecision(
            route,
            confidence,
            intent=intent,
            emotion=emotion,
            tool_call=tool_call,
            trusted_metadata=True,
            reason="router",
        )

    def _validate_tool_call(self, tool_call: Any) -> None:
        if not isinstance(tool_call, dict):
            raise ValueError("tool_local requires tool_call")
        name = tool_call.get("name")
        if not isinstance(name, str) or name not in self._allowlist:
            raise ValueError("tool not allowlisted")
        if self._registry is None or name not in self._registry.names():
            raise ValueError("tool not registered")
        arguments = tool_call.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be object")


def is_crisis(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in CRISIS_KEYWORDS)


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
        raise ValueError("router output must be object")
    return data
