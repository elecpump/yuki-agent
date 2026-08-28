"""Shared decision trace and final reply payload contracts."""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DecisionTrace:
    ts: float
    trigger: str
    emotion: str
    actions: Sequence[object]
    rendered: str
    reason: str
    route: str
    reply_id: str | None
    cooldown_state: dict[str, object]
    situation_provenance: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "emotion": self.emotion,
            "actions": [
                action.name if hasattr(action, "name") else str(action)
                for action in self.actions
            ],
            "rendered": self.rendered,
            "reason": self.reason,
            "route": self.route,
            "reply_id": self.reply_id,
            "cooldown_state": self.cooldown_state,
            "situation_provenance": self.situation_provenance,
        }


def final_reply_payload(
    text: str,
    ts: float,
    emotion: str,
    reply_id: str,
) -> dict[str, object]:
    return {
        "text": text,
        "ts": ts,
        "emotion": emotion,
        "kind": "final",
        "reply_id": reply_id,
    }


def situation_provenance(
    situation: dict[str, object] | None,
) -> dict[str, object]:
    if not situation:
        return {}
    keys = (
        "situation_id",
        "frame_id",
        "source_id",
        "scroll_band",
        "observation_reason",
        "frame_ts",
    )
    return {key: situation[key] for key in keys if key in situation}
