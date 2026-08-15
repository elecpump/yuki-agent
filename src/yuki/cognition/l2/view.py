import hashlib
import math
from typing import Callable

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.sensitive import SensitiveFilter
from yuki.memory.manager import MemoryManager

SITUATION_TOKENS = 200
MEMORY_MIN_TOKENS = 200
MAX_UTTERANCE_CHARS = 500
FOLD_UNIT_SIZE = 6
SUMMARIZE_TIMEOUT_S = 2.0
SUMMARIZE_MAX_FAILURES = 3


def estimate_tokens(text: str) -> int:
    """字符启发式估 token（中英混合粗估），零依赖。"""
    return math.ceil(len(text or "") / 1.5)


def _truncate_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


class CloudViewBuilder:
    """L2 提示视图：enrich（折叠/记忆）→ format（填充顺序+最低配额预算）。"""

    def __init__(self, summarize: Callable[[list[str]], str] | None = None, *,
                 max_turns: int = 20, max_tokens: int = 1500,
                 verbatim_turns: int = 4, memory_top_k: int = 3,
                 sensitive_filter: SensitiveFilter | None = None) -> None:
        self._summarize = summarize
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._verbatim_turns = verbatim_turns
        self._memory_top_k = memory_top_k
        self._sensitive_filter = sensitive_filter or SensitiveFilter()
        self._summary_cache: dict[str, str] = {}
        self._summarize_failures = 0
        self._summarize_broken = False

    def enrich(self, snapshot: ContextSnapshot, memory: MemoryManager | None,
               utterance: str) -> ContextSnapshot:
        safe_turns = tuple(
            t for t in snapshot.recent_turns
            if not self._sensitive_filter.is_sensitive(t.get("content", ""))
        )
        summaries = self._fold(safe_turns, utterance)
        memories = self._retrieve_memory(memory, utterance)
        return ContextSnapshot(
            situation=snapshot.situation,
            recent_turns=safe_turns,
            summaries=tuple(summaries),
            long_term_memory=tuple(memories),
        )

    def _retrieve_memory(self, memory, utterance) -> list[dict]:
        if memory is None:
            return []
        results = memory.query(utterance or "", top_k=self._memory_top_k, min_sensitivity=0)
        safe = [m for m in results if m.get("sensitivity", 0) != 2]
        guaranteed = [m for m in safe if m.get("memory_type") == "preference" or m.get("strengthened")]
        others = [m for m in safe if m not in guaranteed]
        return guaranteed[: self._memory_top_k] + others[: max(0, self._memory_top_k - len(guaranteed))]

    def _fold(self, recent_turns, utterance) -> list[str]:
        fold = list(reversed(recent_turns))[: max(0, len(recent_turns) - self._verbatim_turns)]
        if not fold:
            return []
        # 预算触发：逐字包含折叠轮仍在预算内 → 不折叠
        base = estimate_tokens(utterance or "") + SITUATION_TOKENS
        base += sum(estimate_tokens(t["content"]) for t in recent_turns[: self._verbatim_turns])
        verbatim_fold = sum(estimate_tokens(t["content"]) for t in fold)
        if base + verbatim_fold <= self._max_tokens:
            return []
        segments = [fold[i:i + FOLD_UNIT_SIZE] for i in range(0, len(fold), FOLD_UNIT_SIZE)]
        summaries = []
        used = base
        for seg in segments:
            key = self._segment_key(seg)
            cached = self._summary_cache.get(key)
            if cached is not None:
                text = cached
            else:
                text = self._summarize_segment(seg)
                if text is not None:
                    self._summary_cache[key] = text
            if text is None:
                text = f"（之前聊了 {len(seg)} 轮）"
            tok = estimate_tokens(text)
            if used + tok > self._max_tokens:
                break
            summaries.append(text)
            used += tok
        return summaries

    def _summarize_segment(self, seg) -> str | None:
        if self._summarize is None or self._summarize_broken:
            return None
        try:
            text = self._summarize([t["content"] for t in seg])
            self._summarize_failures = 0
            return text
        except Exception:
            self._summarize_failures += 1
            if self._summarize_failures >= SUMMARIZE_MAX_FAILURES:
                self._summarize_broken = True
            return None

    def _segment_key(self, seg) -> str:
        h = hashlib.sha256()
        for t in seg:
            h.update(t["content"].encode("utf-8"))
        return h.hexdigest()

    def format(self, snapshot: ContextSnapshot, utterance: str) -> str:
        parts = []
        used = 0
        utt = _truncate_chars(utterance or "", MAX_UTTERANCE_CHARS)
        parts.append(f"用户说：{utt}")
        used += estimate_tokens(utt)
        if snapshot.situation:
            sit = self._format_situation(snapshot.situation)
            parts.append(f"当前情境：{sit}")
            used += estimate_tokens(sit)
        for t in snapshot.recent_turns[: self._verbatim_turns]:
            line = f"[{t['kind']}] {t['content']}"
            parts.append(line)
            used += estimate_tokens(line)
        for s in snapshot.summaries:
            line = f"（摘要）{s}"
            if used + estimate_tokens(line) > self._max_tokens:
                break
            parts.append(line)
            used += estimate_tokens(line)
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
        return "\n".join(p for p in parts if p)

    def _format_situation(self, situation: dict) -> str:
        bits = [b for b in [
            situation.get("topic", ""),
            situation.get("summary", ""),
            *(situation.get("key_points") or []),
        ] if b]
        text = " ".join(bits)
        return _truncate_chars(text, int(SITUATION_TOKENS * 1.5))
