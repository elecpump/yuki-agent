import sqlite3

import pytest

from yuki.cognition.context.store import ThreadTurnStore
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.working import WorkingContext


def test_thread_turns_survive_store_restart(tmp_path):
    path = tmp_path / "memory.db"
    store = ThreadTurnStore(path)
    user_turn_id = store.add_user("你好", at=100.0, request_id="request-1")
    store.add_agent("我在", at=101.0, reply_to_turn_id=user_turn_id)
    store.close()

    reopened = ThreadTurnStore(path)
    try:
        turns = reopened.items()
        assert [(turn["kind"], turn["content"]) for turn in turns] == [
            ("agent", "我在"),
            ("user", "你好"),
        ]
        assert turns[0]["reply_to_turn_id"] == user_turn_id
        assert turns[1]["response_state"] == "completed"
    finally:
        reopened.close()


def test_segment_length_and_episode_idle_are_independent_boundaries(tmp_path):
    store = ThreadTurnStore(
        tmp_path / "memory.db",
        segment_max_turns=2,
        episode_idle_s=300,
    )
    try:
        first_user_id = store.add_user("第一问", at=100.0)
        store.add_agent("第一答", at=101.0, reply_to_turn_id=first_user_id)
        second_user_id = store.add_user("继续聊", at=102.0)
        store.add_agent("继续答", at=103.0, reply_to_turn_id=second_user_id)
        third_user_id = store.add_user("隔了很久", at=500.0)

        turns = {turn["id"]: turn for turn in store.items()}

        assert turns[first_user_id]["segment_id"] == turns[first_user_id + 1]["segment_id"]
        assert turns[second_user_id]["segment_id"] != turns[first_user_id]["segment_id"]
        assert turns[first_user_id]["episode_id"] == turns[second_user_id]["episode_id"]
        assert turns[third_user_id]["episode_id"] != turns[second_user_id]["episode_id"]
    finally:
        store.close()


def test_persistent_working_context_close_is_idempotent(tmp_path):
    context = WorkingContext(ThreadTurnStore(tmp_path / "memory.db"))

    context.close()
    context.close()


def test_proactive_turn_joins_active_user_episode(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db")
    try:
        user_turn_id = store.add_user("先聊到这里", at=100.0)
        proactive_turn_id = store.add_agent("顺便提醒一下", at=101.0)

        turns = {turn["id"]: turn for turn in store.items()}
        assert turns[proactive_turn_id]["source"] == "proactive"
        assert turns[proactive_turn_id]["episode_id"] == turns[user_turn_id]["episode_id"]
    finally:
        store.close()


def test_late_reply_does_not_rewrite_terminal_response_state(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db")
    try:
        user_turn_id = store.add_user("这次请求会失败", at=100.0)
        store.mark_response(user_turn_id, "failed")

        store.add_agent("迟到的回复", at=101.0, reply_to_turn_id=user_turn_id)

        turns = {turn["id"]: turn for turn in store.items()}
        assert turns[user_turn_id]["response_state"] == "failed"
    finally:
        store.close()


def test_thread_schema_contains_maintenance_and_candidate_tables(tmp_path):
    path = tmp_path / "memory.db"
    store = ThreadTurnStore(path)
    store.close()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "consolidation_runs",
        "memory_candidates",
        "memory_history",
        "memory_key_aliases",
        "embedding_outbox",
    } <= tables


def test_restart_recovers_expired_maintenance_leases(tmp_path):
    path = tmp_path / "memory.db"
    store = ThreadTurnStore(path, segment_max_turns=1, episode_idle_s=10)
    store.add_user("第一段", at=100.0)
    store.add_user("第二段", at=200.0)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE segments SET summary_state = 'running' WHERE state = 'closed'"
        )
        connection.execute(
            "UPDATE episodes SET state = 'consolidating' WHERE id = 1"
        )
        connection.execute(
            """
            UPDATE consolidation_runs
            SET state = 'leased', lease_until = 0, updated_at = 200.0
            WHERE episode_id = 1
            """
        )

    reopened = ThreadTurnStore(path)
    reopened.close()
    with sqlite3.connect(path) as connection:
        segment_states = {
            row[0] for row in connection.execute("SELECT summary_state FROM segments")
        }
        episode_state = connection.execute(
            "SELECT state FROM episodes WHERE id = 1"
        ).fetchone()[0]
        run_state, lease_until = connection.execute(
            "SELECT state, lease_until FROM consolidation_runs WHERE episode_id = 1"
        ).fetchone()

    assert "running" not in segment_states
    assert episode_state == "closed"
    assert run_state == "pending"
    assert lease_until is None


def test_closed_segment_can_be_claimed_and_completed_with_persistent_summary(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=2)
    context = WorkingContext(store)
    user_turn_id = store.add_user("第一问", at=100.0)
    store.add_agent("第一答", at=101.0, reply_to_turn_id=user_turn_id)

    job = store.claim_segment_summary()
    assert job is not None
    assert [turn["content"] for turn in job.turns] == ["第一问", "第一答"]

    store.complete_segment_summary(
        job.segment_id,
        "用户提出第一问，agent 已回答。",
        model="test-model",
        prompt_version="segment-summary-v1",
        attempt=job.attempt,
    )

    snapshot = ContextProjector().build(context)
    assert snapshot.summaries == ("用户提出第一问，agent 已回答。",)
    assert snapshot.fallback_turns == ()
    context.close()


def test_segment_summary_failures_retry_then_keep_raw_placeholder_fallback(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    context = WorkingContext(store)
    store.add_user("必须保留的原文", at=100.0)

    first_job = store.claim_segment_summary()
    store.fail_segment_summary(
        first_job.segment_id,
        attempt=first_job.attempt,
        max_failures=2,
    )
    second_job = store.claim_segment_summary()
    assert second_job.attempt == 2

    store.fail_segment_summary(
        second_job.segment_id,
        attempt=second_job.attempt,
        max_failures=2,
    )

    assert store.claim_segment_summary() is None
    snapshot = ContextProjector().build(context)
    assert snapshot.summaries == ()
    assert [turn["content"] for turn in snapshot.fallback_turns] == ["必须保留的原文"]
    context.close()


def test_segment_summary_claim_uses_database_lease_and_reclaims_after_expiry(tmp_path):
    path = tmp_path / "memory.db"
    first_store = ThreadTurnStore(path, segment_max_turns=1)
    second_store = ThreadTurnStore(path, segment_max_turns=1)
    first_store.add_user("需要租约", at=100.0)

    first_job = first_store.claim_segment_summary(at=100.0, lease_s=10.0)

    assert first_job is not None
    assert second_store.claim_segment_summary(at=105.0, lease_s=10.0) is None
    reclaimed = second_store.claim_segment_summary(at=111.0, lease_s=10.0)
    assert reclaimed is not None
    assert reclaimed.segment_id == first_job.segment_id
    assert reclaimed.attempt == 2
    with pytest.raises(ValueError, match="segment lease is stale"):
        first_store.complete_segment_summary(
            first_job.segment_id,
            "旧工作者的结果",
            model="test",
            prompt_version="v1",
            attempt=first_job.attempt,
        )
    first_store.close()
    second_store.close()
