import sqlite3

from yuki.cognition.context.store import ThreadTurnStore
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
            INSERT INTO consolidation_runs (
                episode_id, state, lease_until, updated_at
            ) VALUES (1, 'leased', 0, 200.0)
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
