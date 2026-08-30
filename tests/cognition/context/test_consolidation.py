import json
import sqlite3
from dataclasses import replace

import pytest

from yuki.cognition.context.consolidation import (
    CandidateResolver,
    ConsolidationStore,
    EvolutionPolicy,
)
from yuki.cognition.context.sediment import validate_candidates
from yuki.cognition.context.store import ThreadTurnStore
from yuki.memory.manager import MemoryManager
from yuki.memory.provenance import AUTOMATIC_STRENGTHENER
from yuki.memory.store import MemoryStore


def _stores(tmp_path, *, episode_idle_s: float = 10.0):
    path = tmp_path / "memory.db"
    memory = MemoryStore(path)
    thread = ThreadTurnStore(path, episode_idle_s=episode_idle_s)
    consolidation = ConsolidationStore(path)
    return path, memory, thread, consolidation


def _close_episode(thread: ThreadTurnStore, content: str, *, at: float) -> None:
    user_id = thread.add_user(content, at=at)
    thread.add_agent("知道了", at=at + 1, reply_to_turn_id=user_id)
    assert thread.close_idle_episode(at=at + 20) is not None


def _candidate(job, raw: dict):
    return validate_candidates(raw_candidates=[raw], turns=job.turns, related=job.related)[0]


def test_consolidation_claim_has_database_lease_and_attempt_fencing(tmp_path):
    path, memory, thread, first = _stores(tmp_path)
    second = ConsolidationStore(path)
    _close_episode(thread, "我喜欢茶", at=100.0)

    first_job = first.claim(at=121.0, lease_s=10.0)

    assert first_job is not None
    assert second.claim(at=125.0, lease_s=10.0) is None
    reclaimed = second.claim(at=132.0, lease_s=10.0)
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    with pytest.raises(ValueError, match="stale"):
        first.complete(first_job, [])
    first.close()
    second.close()
    thread.close()
    memory.close()


def test_resolver_refuses_ambiguous_semantic_merge():
    scores = {"rpg": 0.92, "桌游": 0.90}
    resolver = CandidateResolver(
        similarity=lambda proposed, existing: scores.get(existing, 0.0),
        threshold=0.88,
        competition_margin=0.03,
    )

    resolved = resolver.resolve(
        "游戏",
        "用户喜欢游戏",
        {"rpg": ["角色扮演"], "桌游": ["棋盘游戏"]},
    )

    assert resolved is None


def test_claim_uses_episode_topic_to_retrieve_related_active_memories(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    relevant_id = memory.create("preference", "用户以前喜欢喝茶")
    for index in range(8):
        memory.create("scenario", f"无关的新事件 {index}")
    store.close()
    seen = {}

    def related_provider(turns: list[dict], limit: int) -> list[dict]:
        seen["text"] = " ".join(turn["content"] for turn in turns)
        seen["limit"] = limit
        return [memory.get(relevant_id)]

    store = ConsolidationStore(path, related_provider=related_provider)
    _close_episode(thread, "我现在不喝茶了", at=100.0)

    job = store.claim(at=121.0)

    assert "不喝茶" in seen["text"]
    assert seen["limit"] == 8
    assert [item["id"] for item in job.related] == [relevant_id]
    store.close()
    thread.close()
    memory.close()


def test_scenario_candidate_is_applied_atomically_with_history_and_outbox(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    _close_episode(thread, "今天去了西湖", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "west-lake",
            "proposed_op": "add",
            "memory_type": "scenario",
            "canonical_key": "西湖游览",
            "content": "用户今天去了西湖",
            "confidence": 0.8,
            "sensitivity": 0,
            "evidence": [{"turn_id": job.turns[0]["id"], "quote": "今天去了西湖"}],
            "metadata": {},
        },
    )

    store.complete(job, [candidate])

    assert [item["content"] for item in memory.list()] == ["用户今天去了西湖"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT state FROM memory_candidates").fetchone()[0] == "applied"
        assert connection.execute("SELECT operation FROM memory_history").fetchone()[0] == "create"
        outbox_operation = connection.execute(
            "SELECT operation FROM embedding_outbox"
        ).fetchone()[0]
        assert outbox_operation == "upsert"
        assert connection.execute("SELECT state FROM episodes").fetchone()[0] == "consolidated"
    assert MemoryManager(memory).process_embedding_outbox() == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM embedding_outbox").fetchone()[0] == 0
    store.close()
    thread.close()
    memory.close()


def test_consolidation_strips_reserved_provenance_at_persistence_boundary(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    _close_episode(thread, "今天去了西湖", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "west-lake",
            "proposed_op": "add",
            "memory_type": "scenario",
            "canonical_key": "西湖游览",
            "content": "用户今天去了西湖",
            "confidence": 0.8,
            "sensitivity": 0,
            "evidence": [{"turn_id": job.turns[0]["id"], "quote": "今天去了西湖"}],
            "metadata": {},
        },
    )
    candidate = replace(
        candidate,
        metadata={
            "strengthened_by": AUTOMATIC_STRENGTHENER,
            "strengthened_episode_count": 99,
            "safe": "kept",
        },
    )

    store.complete(job, [candidate])

    assert memory.list()[0]["metadata"]["safe"] == "kept"
    assert "strengthened_by" not in memory.list()[0]["metadata"]
    with sqlite3.connect(path) as connection:
        metadata = json.loads(
            connection.execute("SELECT metadata FROM memory_candidates").fetchone()[0]
        )
    assert metadata == {"safe": "kept"}
    store.close()
    thread.close()
    memory.close()


def test_preference_stays_candidate_then_promotes_across_two_episodes(tmp_path):
    _, memory, thread, store = _stores(tmp_path)
    policy = EvolutionPolicy(promotion_min_episodes=2, explicit_activation_confidence=0.9)
    store.policy = policy
    for index, at in enumerate((100.0, 200.0), start=1):
        _close_episode(thread, "我喜欢喝茶", at=at)
        job = store.claim(at=at + 21)
        candidate = _candidate(
            job,
            {
                "draft_key": f"tea-{index}",
                "proposed_op": "add",
                "memory_type": "preference",
                "canonical_key": "饮料偏好",
                "content": "用户喜欢喝茶",
                "confidence": 0.8,
                "sensitivity": 0,
                "evidence": [{"turn_id": job.turns[0]["id"], "quote": "我喜欢喝茶"}],
                "metadata": {},
            },
        )
        store.complete(job, [candidate])
        if index == 1:
            assert memory.list() == []

    assert len(memory.list(memory_type="preference")) == 1
    store.close()
    thread.close()
    memory.close()


def test_preference_is_marked_as_automatically_strengthened(tmp_path):
    _, memory, thread, store = _stores(tmp_path)
    store.policy = EvolutionPolicy(
        promotion_min_episodes=2,
        strengthen_min_episodes=3,
        explicit_activation_confidence=0.9,
    )
    for index, at in enumerate((100.0, 200.0, 300.0), start=1):
        _close_episode(thread, "我喜欢喝茶", at=at)
        job = store.claim(at=at + 21)
        candidate = _candidate(
            job,
            {
                "draft_key": f"tea-{index}",
                "proposed_op": "add",
                "memory_type": "preference",
                "canonical_key": "饮料偏好",
                "content": "用户喜欢喝茶",
                "confidence": 0.8,
                "sensitivity": 0,
                "evidence": [{"turn_id": job.turns[0]["id"], "quote": "我喜欢喝茶"}],
                "metadata": {},
            },
        )
        store.complete(job, [candidate])

    preference = memory.list(memory_type="preference")[0]
    assert preference["strengthened"] is True
    assert preference["metadata"]["strengthened_by"] == AUTOMATIC_STRENGTHENER
    assert preference["metadata"]["strengthened_episode_count"] == 3
    store.close()
    thread.close()
    memory.close()


def test_update_creates_new_revision_and_supersedes_old_row(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    store.policy = EvolutionPolicy(update_min_episodes=1)
    target_id = memory.create(
        "preference",
        "用户喜欢喝茶",
        confidence=0.9,
        metadata={"canonical_key_norm": "饮料"},
    )
    _close_episode(thread, "我现在更喜欢咖啡", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "drink-update",
            "proposed_op": "update",
            "memory_type": "preference",
            "canonical_key": "饮料偏好",
            "content": "用户现在更喜欢咖啡",
            "confidence": 0.96,
            "sensitivity": 0,
            "target_id": target_id,
            "target_revision": 1,
            "evidence": [{"turn_id": job.turns[0]["id"], "quote": "我现在更喜欢咖啡"}],
            "metadata": {},
        },
    )

    store.complete(job, [candidate])

    active = memory.list(memory_type="preference")
    assert len(active) == 1
    assert active[0]["content"] == "用户现在更喜欢咖啡"
    assert active[0]["revision"] == 2
    assert active[0]["supersedes_id"] == target_id
    with sqlite3.connect(path) as connection:
        old_state, old_revision = connection.execute(
            "SELECT state, revision FROM memories WHERE id = ?", (target_id,)
        ).fetchone()
        assert (old_state, old_revision) == ("superseded", 1)
        operations = {row[0] for row in connection.execute("SELECT operation FROM memory_history")}
        assert {"supersede", "update"} <= operations
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM memory_history WHERE operation = 'supersede'"
            ).fetchone()[0]
        )
        assert (snapshot["state"], snapshot["revision"]) == ("active", 1)
    store.close()
    thread.close()
    memory.close()


def test_resolver_persists_unique_high_confidence_key_alias(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    memory.create(
        "preference",
        "用户喜欢 RPG",
        confidence=0.95,
        metadata={"canonical_key_norm": "rpg", "canonical_key": "RPG"},
    )
    resolver = CandidateResolver(threshold=0.88)
    store.resolver = resolver
    _close_episode(thread, "我还是喜欢角色扮演游戏", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "role-playing",
            "proposed_op": "add",
            "memory_type": "preference",
            "canonical_key": "角色扮演游戏",
            "content": "用户喜欢 RPG",
            "confidence": 0.8,
            "sensitivity": 0,
            "evidence": [
                {"turn_id": job.turns[0]["id"], "quote": "喜欢角色扮演游戏"}
            ],
            "metadata": {},
        },
    )

    store.complete(job, [candidate])

    assert len(memory.list(memory_type="preference")) == 1
    with sqlite3.connect(path) as connection:
        alias = connection.execute(
            "SELECT alias_norm, canonical_norm FROM memory_key_aliases"
        ).fetchone()
        assert alias == ("角色扮演游戏", "rpg")
        candidate_norm = connection.execute(
            "SELECT canonical_key_norm FROM memory_candidates"
        ).fetchone()[0]
        assert candidate_norm == "rpg"
    store.close()
    thread.close()
    memory.close()


def test_delete_candidate_tombstones_without_exposing_old_memory(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    store.policy = EvolutionPolicy(tombstone_min_episodes=1)
    target_id = memory.create(
        "preference",
        "用户喜欢喝茶",
        confidence=0.9,
        metadata={"canonical_key_norm": "饮料"},
    )
    _close_episode(thread, "我已经不喜欢喝茶了", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "forget-tea",
            "proposed_op": "delete",
            "memory_type": "preference",
            "canonical_key": "饮料偏好",
            "content": "用户不再喜欢喝茶",
            "confidence": 0.98,
            "sensitivity": 0,
            "target_id": target_id,
            "target_revision": 1,
            "evidence": [{"turn_id": job.turns[0]["id"], "quote": "不喜欢喝茶了"}],
            "metadata": {},
        },
    )

    store.complete(job, [candidate])

    assert memory.list() == []
    with sqlite3.connect(path) as connection:
        state, revision = connection.execute(
            "SELECT state, revision FROM memories WHERE id = ?", (target_id,)
        ).fetchone()
        assert (state, revision) == ("tombstoned", 1)
        assert connection.execute("SELECT operation FROM memory_history").fetchone()[0] == (
            "tombstone"
        )
        assert connection.execute("SELECT operation FROM embedding_outbox").fetchone()[0] == (
            "delete"
        )
        snapshot = json.loads(
            connection.execute("SELECT snapshot_json FROM memory_history").fetchone()[0]
        )
        assert (snapshot["state"], snapshot["revision"]) == ("active", 1)
    store.close()
    thread.close()
    memory.close()


def test_consolidation_transaction_rolls_back_every_candidate_side_effect(tmp_path):
    path, memory, thread, store = _stores(tmp_path)
    _close_episode(thread, "今天去了西湖", at=100.0)
    job = store.claim(at=121.0)
    candidate = _candidate(
        job,
        {
            "draft_key": "west-lake",
            "proposed_op": "add",
            "memory_type": "scenario",
            "canonical_key": "西湖游览",
            "content": "用户今天去了西湖",
            "confidence": 0.8,
            "sensitivity": 0,
            "evidence": [{"turn_id": job.turns[0]["id"], "quote": "今天去了西湖"}],
            "metadata": {},
        },
    )
    def crash_before_watermark(boundary: str) -> None:
        if boundary == "before_watermark":
            raise RuntimeError("crash")

    store.fault_injector = crash_before_watermark

    with pytest.raises(RuntimeError, match="crash"):
        store.complete(job, [candidate])

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_candidates").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_history").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM embedding_outbox").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM consolidation_runs").fetchone()[0] == (
            "leased"
        )
    store.close()
    thread.close()
    memory.close()
