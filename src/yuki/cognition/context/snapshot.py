from dataclasses import dataclass

from yuki.cognition.context.working import WorkingContext


@dataclass(frozen=True)
class ContextSnapshot:
    """每次决策投影的只读快照。决策层只见此 schema。"""

    situation: dict | None = None
    recent_turns: tuple = ()
    summaries: tuple = ()
    fallback_turns: tuple = ()
    long_term_memory: tuple = ()


class ContextProjector:
    """把写入侧投影为只读快照（裁剪/排序/去重）。"""

    def __init__(
        self,
        max_turns: int = 20,
        *,
        fallback_turns: int = 8,
        max_summaries: int = 8,
        summary_max_tokens: int = 600,
    ) -> None:
        self._max_turns = max_turns
        self._fallback_turns = fallback_turns
        self._max_summaries = max_summaries
        self._summary_max_tokens = summary_max_tokens

    def build(
        self,
        working: WorkingContext,
        *,
        exclude_turn_id: int | None = None,
    ) -> ContextSnapshot:
        recent_items, summary_items, fallback_items = working.projection_items()
        turns = self._turns(recent_items, exclude_turn_id, self._max_turns)
        fallback = self._turns(fallback_items, exclude_turn_id, self._fallback_turns)
        return ContextSnapshot(
            situation=working.situation(),
            recent_turns=tuple(turns),
            summaries=tuple(self._summaries(summary_items)),
            fallback_turns=tuple(fallback),
        )

    def _summaries(self, items: list[str]) -> list[str]:
        summaries = []
        used = 0
        for summary in items[: self._max_summaries]:
            tokens = max(1, (len(summary) + 1) // 2)
            if used + tokens > self._summary_max_tokens:
                break
            summaries.append(summary)
            used += tokens
        return summaries

    @staticmethod
    def _turns(items: list[dict], exclude_turn_id: int | None, limit: int) -> list[dict]:
        seen = None
        turns = []
        for item in items:  # 新→旧
            if exclude_turn_id is not None and item.get("id") == exclude_turn_id:
                continue
            content = item.get("content", "")
            if content and content != seen:
                turns.append({
                    "content": content,
                    "kind": item.get("kind", "turn"),
                    "ts": item.get("ts", 0.0),
                })
            seen = content
            if len(turns) >= limit:
                break
        return turns
