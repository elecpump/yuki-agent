from enum import Enum

from yuki.cognition.brain.actions import Action
from yuki.cognition.brain.classifier import Emotion, Intent


class TriggerKind(str, Enum):
    UTTERANCE = "utterance"
    AWAKE = "awake"
    SITUATION = "situation"


class Tier(str, Enum):
    L1 = "l1"
    L2 = "l2"


FAREWELL_KEYWORDS = ("再见", "晚安", "拜拜", "下次聊")

# 披露类意图（emotional/companion）追加 write_memory 副动作
DISCLOSURE_INTENTS = (Intent.EMOTIONAL, Intent.COMPANION)

DEFAULT_POLICY_TABLE: dict[Intent, list[str]] = {
    Intent.CHIT_CHAT: ["inform"],
    Intent.EMOTIONAL: ["empathize", "ask"],
    Intent.ENTERTAINMENT: ["joke"],
    Intent.GAME: ["invite_game"],
    Intent.ROLEPLAY: ["inform"],
    Intent.CREATIVE: ["inform"],
    Intent.COMPANION: ["acknowledge", "ask"],
    Intent.SYSTEM: ["inform"],
    Intent.SAFETY: ["safety_escalate"],
    Intent.UNKNOWN: ["clarify"],
}

L2_INTENTS = {Intent.ENTERTAINMENT, Intent.CREATIVE, Intent.ROLEPLAY, Intent.GAME, Intent.EMOTIONAL}


class DecisionPolicy:
    """意图/触发 → 动作序列。UTTERANCE 按策略表;SITUATION 走主动开口冷却门控。"""

    def __init__(
        self,
        proactive_cooldown_s: float,
        *,
        proactive_enabled: bool = True,
        policy_table: dict[Intent, list[str]] | None = None,
    ) -> None:
        self._cooldown = proactive_cooldown_s
        self._enabled = proactive_enabled
        self._table = policy_table if policy_table is not None else DEFAULT_POLICY_TABLE

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def set_cooldown_s(self, value: float) -> None:
        self._cooldown = value

    def tier_for(self, intent: Intent) -> Tier:
        return Tier.L2 if intent in L2_INTENTS else Tier.L1

    def decide(
        self,
        trigger: TriggerKind,
        intent: Intent,
        emotion: Emotion,
        text: str = "",
        situation: dict | None = None,
        last_open_ts: float | None = None,
        now: float = 0.0,
    ) -> list[Action]:
        if trigger == TriggerKind.AWAKE:
            return [Action("inform")]
        if trigger == TriggerKind.SITUATION:
            return self._decide_situation(situation, last_open_ts, now)
        return self._decide_utterance(intent, text)

    def _decide_utterance(self, intent: Intent, text: str) -> list[Action]:
        if intent == Intent.SAFETY:
            return [Action("safety_escalate")]
        if intent == Intent.SYSTEM and any(kw in text for kw in FAREWELL_KEYWORDS):
            return [Action("farewell")]
        names = self._table.get(intent, ["inform"])
        actions = [Action(name) for name in names]
        if intent in DISCLOSURE_INTENTS:
            actions.append(Action("write_memory", {
                "memory_type": "preference",
                "content": text,
            }))
        return actions

    def _decide_situation(
        self,
        situation: dict | None,
        last_open_ts: float | None,
        now: float,
    ) -> list[Action]:
        if not self._enabled:
            return [Action("stay_silent")]
        if situation is None or situation.get("sensitive") or not situation.get("topic"):
            return [Action("stay_silent")]
        if last_open_ts is not None and now - last_open_ts < self._cooldown:
            return [Action("stay_silent")]
        return [Action("acknowledge", {"topic": situation.get("topic")}), Action("ask")]
