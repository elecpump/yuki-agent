"""Cloud decision agent for optional proactive speech."""

import json
from dataclasses import dataclass

from yuki.cognition.brain.persona import format_core_values, format_personality_traits
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.view import CloudViewBuilder
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.l2.proactive")

PROACTIVE_RULES = """【主动开口准则】
- 只有对用户当前正在看/做的事有真正值得说的内容时才 speak。
- speak 时 1-2 句，自然口语，像朋友随口搭话；不评价用户；不转述屏幕内容本身。
- 没有值得说的就 silent。silent 是完全正常且受鼓励的输出，不要找话说。
- 用户可能正在专注工作或阅读，不确定时倾向 silent。
- 只输出 JSON，不要输出其他文字。"""


@dataclass(frozen=True)
class ProactiveDecision:
    action: str
    text: str
    reason: str
    raw: str | None


class ProactiveAgent:
    """Ask the cloud model whether the current moment merits speaking."""

    def __init__(
        self,
        client: CloudClient,
        *,
        system_prompt: str | None = None,
        view_builder: CloudViewBuilder | None = None,
        timeout_s: float = 5.0,
        max_chars: int = 200,
    ) -> None:
        self._client = client
        self._system_prompt = system_prompt
        self._view_builder = view_builder or CloudViewBuilder()
        self._timeout_s = timeout_s
        self._max_chars = max(1, int(max_chars))

    def decide(
        self,
        snapshot: ContextSnapshot,
        soul: dict | None = None,
    ) -> ProactiveDecision:
        messages = self._messages(snapshot, soul)
        try:
            response = self._client.chat(
                messages,
                timeout_s=self._timeout_s,
                temperature=0.5,
                max_tokens=100,
            )
            raw = self._content(response)
        except CloudError:
            logger.warning("proactive cloud decision failed", exc_info=True)
            return ProactiveDecision("silent", "", "cloud_error", None)
        try:
            data = self._parse_object(raw)
            action = data.get("action")
            reason = data.get("reason", "")
            if action not in {"speak", "silent"} or not isinstance(reason, str):
                raise ValueError("invalid proactive decision fields")
            text = data.get("text", "")
            if not isinstance(text, str):
                raise ValueError("invalid proactive text")
            text = text.strip()
            if action == "speak" and not text:
                raise ValueError("speak decision requires text")
            return ProactiveDecision(action, text[: self._max_chars], reason.strip(), raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("invalid proactive decision", raw=raw)
            return ProactiveDecision("silent", "", "parse_error", raw)

    def _messages(self, snapshot: ContextSnapshot, soul: dict | None) -> list[dict]:
        soul = soul or {}
        description = str(soul.get("personality_description", "")).strip()
        traits = format_personality_traits(soul.get("personality_traits") or {})
        binding_values = [
            value
            for value in soul.get("core_values") or []
            if isinstance(value, dict) and value.get("role") == "binding"
        ]
        core = format_core_values(binding_values)
        personality = "\n\n".join(part for part in (description, traits, core) if part)
        instruction = self._system_prompt or (
            "你负责判断陪伴型 agent 此刻是否值得主动开口，并生成严格 JSON："
            '{"action":"speak|silent","text":"","reason":""}。'
        )
        system = "\n\n".join(
            part for part in (instruction, f"人格与约束：\n{personality}", PROACTIVE_RULES) if part
        )
        view = self._view_builder.format(snapshot, "")
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "情境：用户在浏览一篇刚出现明显反转的文章。",
            },
            {
                "role": "assistant",
                "content": (
                    '{"action":"speak","text":"这个转折还挺突然的，前面的伏笔你注意到了吗？",'
                    '"reason":"有自然且具体的切入点"}'
                ),
            },
            {"role": "user", "content": "情境：桌面没有有意义的变化，也没有新的交流。"},
            {
                "role": "assistant",
                "content": '{"action":"silent","text":"","reason":"没有值得打扰用户的内容"}',
            },
            {"role": "user", "content": view},
        ]

    @staticmethod
    def _content(response: dict) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudError("invalid response: no message content") from exc
        if not isinstance(content, str) or not content.strip():
            raise CloudError("invalid response: empty message content")
        return content

    @staticmethod
    def _parse_object(raw: str) -> dict:
        text = raw.strip()
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
            raise TypeError("proactive output must be an object")
        return data
