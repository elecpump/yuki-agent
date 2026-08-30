AUTOMATIC_STRENGTHENER = "memory_evolver"


def is_automatic_personality_evidence(memory: dict) -> bool:
    """Whether one active memory may influence long-term personality evolution."""
    metadata = memory.get("metadata") or {}
    return (
        memory.get("memory_type") == "preference"
        and bool(memory.get("strengthened"))
        and metadata.get("strengthened_by") == AUTOMATIC_STRENGTHENER
    )
