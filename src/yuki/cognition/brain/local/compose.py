from contextlib import nullcontext

from yuki.cognition.brain.local.router import CRISIS_KEYWORDS, is_crisis
from yuki.cognition.brain.persona import DEFAULT_BASE_PROMPT
from yuki.cognition.call_tracker import CallTracker
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import estimate_tokens
from yuki.memory.privacy import MemoryAccess, MemoryPurpose


class LocalViewBuilder:
    def __init__(
        self,
        *,
        max_tokens: int = 6000,
        verbatim_turns: int = 5,
        memory_top_k: int = 3,
        crisis_keywords: tuple[str, ...] = CRISIS_KEYWORDS,
    ) -> None:
        self._max_tokens = max_tokens
        self._verbatim_turns = verbatim_turns
        self._memory_top_k = memory_top_k
        self._crisis_keywords = crisis_keywords

    def build(self, snapshot: ContextSnapshot | None, memory, utterance: str) -> str:
        snapshot = snapshot or ContextSnapshot()
        parts = []
        utterance_line = self._fit_required("用户：", (utterance or "")[:1200])
        used = estimate_tokens(utterance_line)

        def add(label: str, text: str) -> None:
            nonlocal used
            if not text:
                return
            line = f"{label}{text}"
            tokens = estimate_tokens(line)
            if used + tokens <= self._max_tokens:
                parts.append(line)
                used += tokens

        situation = snapshot.situation or {}
        if situation:
            bits = [situation.get("topic", ""), situation.get("summary", "")]
            bits.extend(situation.get("key_points") or [])
            add("当前情境：", " ".join(str(bit) for bit in bits if bit)[:1000])

        for summary in snapshot.summaries:
            add("（摘要）", str(summary))

        for turn in snapshot.fallback_turns:
            add(
                f"（历史原文）[{turn.get('kind', 'turn')}] ",
                str(turn.get("content", ""))[:1200],
            )

        for turn in list(snapshot.recent_turns or ())[: self._verbatim_turns]:
            content = str(turn.get("content", ""))
            if self._contains_crisis(content):
                continue
            add(f"[{turn.get('kind', 'turn')}] ", content[:1200])

        for turn in list(snapshot.recent_turns or ())[self._verbatim_turns :]:
            content = str(turn.get("content", ""))
            if self._contains_crisis(content):
                continue
            add(f"[{turn.get('kind', 'turn')}] ", content[:1200])

        for memory_item in self._memories(memory, utterance):
            add("- 记忆：", str(memory_item.get("content", ""))[:800])

        parts.append(utterance_line)
        return "\n".join(part for part in parts if part)

    def _fit_required(self, label: str, text: str) -> str:
        line = f"{label}{text}"
        if estimate_tokens(line) <= self._max_tokens:
            return line
        max_chars = max(1, int(self._max_tokens * 1.5) - len(label))
        clipped = text[:max_chars]
        line = f"{label}{clipped}"
        while clipped and estimate_tokens(line) > self._max_tokens:
            clipped = clipped[:-1]
            line = f"{label}{clipped}"
        return line

    def _memories(self, memory, utterance: str) -> list[dict]:
        if memory is None:
            return []
        access = MemoryAccess(memory)
        queried = access.query(
            utterance or "",
            purpose=MemoryPurpose.LOCAL_MODEL_CONTEXT,
            top_k=self._memory_top_k,
            min_sensitivity=0,
        )
        seen = {item.get("id") for item in queried}
        guaranteed = [
            item
            for item in access.list(
                purpose=MemoryPurpose.LOCAL_MODEL_CONTEXT,
                memory_type="preference",
            )
            if item.get("id") not in seen
        ][: max(0, self._memory_top_k - len(queried))]
        return queried + guaranteed

    def _contains_crisis(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword.lower() in lowered for keyword in self._crisis_keywords)


class LocalComposer:
    def __init__(
        self,
        model,
        *,
        persona_name: str = "yuki",
        system_prompt: str | None = None,
        view_builder: LocalViewBuilder | None = None,
        reply_max_tokens: int = 256,
        timeout_ms: int = 700,
        model_registry: CallTracker | None = None,
        model_name: str = "local_chat",
    ) -> None:
        self._model = model
        self._system = system_prompt or DEFAULT_BASE_PROMPT.format(persona=persona_name)
        self._view_builder = view_builder or LocalViewBuilder()
        self._reply_max_tokens = reply_max_tokens
        self._timeout_ms = timeout_ms
        self._model_registry = model_registry
        self._model_name = model_name

    def set_system_prompt(self, text: str) -> None:
        self._system = text

    def generate(self, utterance: str, snapshot=None, memory=None) -> str:
        if is_crisis(utterance):
            raise RuntimeError("crisis input must not be answered by local model")
        view = self._view_builder.build(snapshot, memory, utterance)
        with self._model_call_tracker():
            reply = self._model.generate(
                [
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": view},
                ],
                max_new_tokens=self._reply_max_tokens,
                timeout_ms=self._timeout_ms,
            )
        return (reply or "").strip()

    def _model_call_tracker(self):
        if self._model_registry is None:
            return nullcontext()
        return self._model_registry.track_call(self._model_name)
