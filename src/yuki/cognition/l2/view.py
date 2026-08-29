import math

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPurpose

SITUATION_TOKENS = 200
MEMORY_MIN_TOKENS = 200
MAX_UTTERANCE_CHARS = 500


def estimate_tokens(text: str) -> int:
    """字符启发式估 token（中英混合粗估），零依赖。"""
    return math.ceil(len(text or "") / 1.5)


def _truncate_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


class CloudViewBuilder:
    """L2 prompt projection over persistent summaries, turns, and memories."""

    def __init__(
        self,
        *,
        max_tokens: int = 1500,
        verbatim_turns: int = 4,
        memory_top_k: int = 3,
    ) -> None:
        self._max_tokens = max_tokens
        self._verbatim_turns = verbatim_turns
        self._memory_top_k = memory_top_k

    def enrich(self, snapshot: ContextSnapshot, memory: MemoryManager | None,
               utterance: str) -> ContextSnapshot:
        memories = self._retrieve_memory(memory, utterance)
        return ContextSnapshot(
            situation=snapshot.situation,
            recent_turns=snapshot.recent_turns,
            summaries=snapshot.summaries,
            fallback_turns=snapshot.fallback_turns,
            long_term_memory=tuple(memories),
        )

    def _retrieve_memory(self, memory, utterance) -> list[dict]:
        if memory is None:
            return []
        safe = MemoryAccess(memory).query(
            utterance or "",
            purpose=MemoryPurpose.CLOUD_MODEL_CONTEXT,
            top_k=self._memory_top_k,
            min_sensitivity=0,
        )
        guaranteed = [
            memory
            for memory in safe
            if memory.get("memory_type") == "preference" or memory.get("strengthened")
        ]
        others = [m for m in safe if m not in guaranteed]
        remaining = max(0, self._memory_top_k - len(guaranteed))
        return guaranteed[: self._memory_top_k] + others[:remaining]

    def format(self, snapshot: ContextSnapshot, utterance: str) -> str:
        parts = []
        used = 0
        utt = _truncate_chars(utterance or "", MAX_UTTERANCE_CHARS)
        used += estimate_tokens(utt)
        if snapshot.situation:
            sit = self._format_situation(snapshot.situation)
            parts.append(f"当前情境：{sit}")
            used += estimate_tokens(sit)
        for s in snapshot.summaries:
            line = f"（摘要）{s}"
            if used + estimate_tokens(line) > self._max_tokens:
                break
            parts.append(line)
            used += estimate_tokens(line)
        for turn in snapshot.fallback_turns:
            line = f"（历史原文）[{turn['kind']}] {turn['content']}"
            if used + estimate_tokens(line) > self._max_tokens:
                break
            parts.append(line)
            used += estimate_tokens(line)
        for t in snapshot.recent_turns[: self._verbatim_turns]:
            line = f"[{t['kind']}] {t['content']}"
            parts.append(line)
            used += estimate_tokens(line)
        for turn in snapshot.recent_turns[self._verbatim_turns :]:
            line = f"[{turn['kind']}] {turn['content']}"
            tokens = estimate_tokens(line)
            if used + tokens > self._max_tokens:
                break
            parts.append(line)
            used += tokens
        if snapshot.long_term_memory:
            mem_lines, guaranteed_tok = [], 0
            for m in snapshot.long_term_memory:
                line = f"- {m['content']}"
                tok = estimate_tokens(line)
                if (m.get("memory_type") == "preference" or m.get("strengthened")) \
                        and guaranteed_tok < MEMORY_MIN_TOKENS:
                    mem_lines.append((line, tok))
                    guaranteed_tok += tok
            for m in snapshot.long_term_memory:
                line = f"- {m['content']}"
                tok = estimate_tokens(line)
                if m.get("memory_type") == "preference" or m.get("strengthened"):
                    continue
                if used + sum(t for _, t in mem_lines) + tok > self._max_tokens:
                    break
                mem_lines.append((line, tok))
            if mem_lines:
                parts.append("相关记忆：\n" + "\n".join(l for l, _ in mem_lines))
        parts.append(f"用户说：{utt}")
        return "\n".join(p for p in parts if p)

    def _format_situation(self, situation: dict) -> str:
        bits = [b for b in [
            situation.get("topic", ""),
            situation.get("summary", ""),
            *(situation.get("key_points") or []),
        ] if b]
        text = " ".join(bits)
        return _truncate_chars(text, int(SITUATION_TOKENS * 1.5))
