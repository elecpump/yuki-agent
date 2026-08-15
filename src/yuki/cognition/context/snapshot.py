from dataclasses import dataclass

from yuki.cognition.context.working import WorkingContext


@dataclass(frozen=True)
class ContextSnapshot:
    """每次决策投影的只读快照。决策层只见此 schema。"""

    situation: dict | None = None
    recent_turns: tuple = ()
    summaries: tuple = ()
    long_term_memory: tuple = ()


class ContextProjector:
    """把写入侧投影为只读快照（裁剪/排序/去重）。"""

    def __init__(self, max_turns: int = 20) -> None:
        self._max_turns = max_turns

    def build(self, working: WorkingContext) -> ContextSnapshot:
        seen = None
        turns = []
        for item in working.items():  # 新→旧
            content = item.get("content", "")
            if content and content != seen:
                turns.append({
                    "content": content,
                    "kind": item.get("kind", "turn"),
                    "ts": item.get("ts", 0.0),
                })
            seen = content
            if len(turns) >= self._max_turns:
                break
        return ContextSnapshot(situation=working.situation(), recent_turns=tuple(turns))
