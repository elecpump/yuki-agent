from enum import StrEnum

from yuki.memory.manager import MemoryManager


class MemoryPurpose(StrEnum):
    USER_EXPLICIT_VIEW = "user_explicit_view"
    LOCAL_MODEL_CONTEXT = "local_model_context"
    CLOUD_MODEL_CONTEXT = "cloud_model_context"
    PERSONA_REFINE_CLOUD = "persona_refine_cloud"
    LLM_TOOL_QUERY_RESULT = "llm_tool_query_result"


class MemoryPrivacyPolicy:
    """Purpose-aware memory visibility policy.

    Unknown sensitivity or purpose fails closed for model paths.
    """

    def allows(self, memory: dict, purpose: MemoryPurpose | str) -> bool:
        try:
            purpose = MemoryPurpose(purpose)
        except ValueError:
            return False
        try:
            sensitivity = int(memory.get("sensitivity", 0))
        except (TypeError, ValueError):
            sensitivity = 2

        if purpose == MemoryPurpose.USER_EXPLICIT_VIEW:
            return sensitivity in (0, 1, 2)
        if purpose == MemoryPurpose.LOCAL_MODEL_CONTEXT:
            return sensitivity in (0, 1)
        if purpose in (
            MemoryPurpose.CLOUD_MODEL_CONTEXT,
            MemoryPurpose.PERSONA_REFINE_CLOUD,
            MemoryPurpose.LLM_TOOL_QUERY_RESULT,
        ):
            return sensitivity == 0
        return False

    def filter(self, memories: list[dict], purpose: MemoryPurpose | str) -> list[dict]:
        return [memory for memory in memories if self.allows(memory, purpose)]


class MemoryAccess:
    """Read facade that requires a privacy purpose for every memory read."""

    def __init__(
        self,
        manager: MemoryManager,
        *,
        policy: MemoryPrivacyPolicy | None = None,
    ) -> None:
        self._manager = manager
        self._policy = policy or MemoryPrivacyPolicy()

    def query(
        self,
        text: str,
        *,
        purpose: MemoryPurpose | str,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
    ) -> list[dict]:
        candidates = self._manager.query(
            text,
            memory_type=memory_type,
            top_k=max(top_k * 5, top_k),
            min_sensitivity=min_sensitivity,
        )
        return self._policy.filter(candidates, purpose)[:top_k]

    def list(
        self,
        *,
        purpose: MemoryPurpose | str,
        memory_type: str | None = None,
        min_sensitivity: int = 0,
    ) -> list[dict]:
        return self._policy.filter(
            self._manager.list(memory_type=memory_type, min_sensitivity=min_sensitivity),
            purpose,
        )

    def get(self, memory_id: int, *, purpose: MemoryPurpose | str) -> dict | None:
        memory = self._manager.get(memory_id)
        if memory is None or not self._policy.allows(memory, purpose):
            return None
        return memory
