AUTOMATIC_STRENGTHENER = "memory_evolver"
OPERATOR_STRENGTHENER = "operator"
RESERVED_PROVENANCE_KEYS = frozenset(
    {"strengthened_by", "strengthened_episode_count"}
)


def without_reserved_provenance(metadata: dict | None) -> dict:
    """Return untrusted metadata without system-owned personality provenance."""
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key not in RESERVED_PROVENANCE_KEYS
    }


def is_automatic_personality_evidence(memory: dict) -> bool:
    """Whether one active memory may influence long-term personality evolution."""
    metadata = memory.get("metadata") or {}
    return (
        memory.get("memory_type") == "preference"
        and bool(memory.get("strengthened"))
        and metadata.get("strengthened_by") == AUTOMATIC_STRENGTHENER
    )
