import pytest

from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot
from yuki.cognition.context.store import ThreadTurnStore
from yuki.cognition.context.working import WorkingContext


@pytest.fixture()
def context(tmp_path):
    value = WorkingContext(ThreadTurnStore(tmp_path / "m.db"))
    yield value
    value.close()


def test_project_fills_situation_and_recent_turns(context):
    ctx = context
    ctx.update_situation({"topic": "量子计算", "sensitive": False})
    ctx.add_user("你好")
    ctx.add_agent("我在")
    snap = ContextProjector().build(ctx)
    assert snap.situation["topic"] == "量子计算"
    assert [t["content"] for t in snap.recent_turns] == ["我在", "你好"]
    assert [t["kind"] for t in snap.recent_turns] == ["agent", "user"]


def test_project_dedups_consecutive_repeats(context):
    ctx = context
    ctx.add_user("嗯嗯")
    ctx.add_user("嗯嗯")
    ctx.add_agent("好")
    snap = ContextProjector().build(ctx)
    assert [t["content"] for t in snap.recent_turns] == ["好", "嗯嗯"]


def test_project_caps_max_turns(context):
    ctx = context
    for i in range(30):
        ctx.add_user(f"t{i}")
    snap = ContextProjector(max_turns=5).build(ctx)
    assert len(snap.recent_turns) == 5
    assert snap.recent_turns[0]["content"] == "t29"  # 新→旧


def test_snapshot_is_frozen():
    snap = ContextSnapshot(recent_turns=({"content": "x", "kind": "user", "ts": 0.0},))
    with pytest.raises(Exception):
        snap.recent_turns = ()


def test_project_separates_active_segment_from_pending_summary_fallback(tmp_path):
    store = ThreadTurnStore(tmp_path / "thread.db", segment_max_turns=2)
    context = WorkingContext(store)
    first_user_id = store.add_user("上一问", at=100.0)
    store.add_agent("上一答", at=101.0, reply_to_turn_id=first_user_id)
    current_user_id = store.add_user("当前问题", at=102.0)

    snapshot = ContextProjector(fallback_turns=8).build(
        context,
        exclude_turn_id=current_user_id,
    )

    assert snapshot.recent_turns == ()
    assert [turn["content"] for turn in snapshot.fallback_turns] == ["上一答", "上一问"]
    context.close()
