from dataclasses import dataclass
from enum import StrEnum


class TriggerKind(StrEnum):
    UTTERANCE = "utterance"
    AWAKE = "awake"
    SITUATION = "situation"


@dataclass(frozen=True)
class SituationAction:
    name: str
    params: dict | None = None


class DecisionPolicy:
    """Situation-only proactive gate after local-brain route rewrite."""

    def __init__(
        self,
        proactive_cooldown_s: float,
        *,
        proactive_enabled: bool = True,
        binding_core_values: list[dict] | None = None,
    ) -> None:
        self._cooldown = proactive_cooldown_s
        self._enabled = proactive_enabled
        self._binding_blocks: set[str] = set()
        self.set_binding_core_values(binding_core_values or [])

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def set_cooldown_s(self, value: float) -> None:
        self._cooldown = value

    def set_binding_core_values(self, values: list[dict]) -> None:
        blocks = set()
        for value in values:
            if not isinstance(value, dict) or value.get("role") != "binding":
                continue
            for action_name in value.get("blocks") or []:
                if isinstance(action_name, str):
                    blocks.add(action_name)
        self._binding_blocks = blocks

    def decide(
        self,
        trigger: TriggerKind,
        *,
        situation: dict | None = None,
        last_open_ts: float | None = None,
        now: float = 0.0,
    ) -> list[SituationAction]:
        if trigger != TriggerKind.SITUATION:
            return [SituationAction("stay_silent")]
        return self._decide_situation(situation, last_open_ts, now)

    def _decide_situation(
        self,
        situation: dict | None,
        last_open_ts: float | None,
        now: float,
    ) -> list[SituationAction]:
        if not self._enabled:
            return [SituationAction("stay_silent")]
        if situation is None or not situation.get("topic"):
            return [SituationAction("stay_silent")]
        if last_open_ts is not None and now - last_open_ts < self._cooldown:
            return [SituationAction("stay_silent")]
        return self._apply_binding_constraints([
            SituationAction("acknowledge", {"topic": situation.get("topic")}),
            SituationAction("ask"),
        ])

    def _apply_binding_constraints(self, actions: list[SituationAction]) -> list[SituationAction]:
        if not self._binding_blocks:
            return actions
        filtered = [action for action in actions if action.name not in self._binding_blocks]
        return filtered or [SituationAction("stay_silent")]
