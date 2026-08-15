import pytest

from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_ctx(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    return WorkingContext(ShortTermTurnStore(manager), snapshot_path=None)


def test_project_fills_situation_and_recent_turns(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.update_situation({"topic": "量子计算", "sensitive": False})
    ctx.add_user("你好")
    ctx.add_agent("我在")
    snap = ContextProjector().build(ctx)
    assert snap.situation["topic"] == "量子计算"
    assert [t["content"] for t in snap.recent_turns] == ["我在", "你好"]
    assert [t["kind"] for t in snap.recent_turns] == ["agent", "user"]


def test_project_dedups_consecutive_repeats(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.add_user("嗯嗯")
    ctx.add_user("嗯嗯")
    ctx.add_agent("好")
    snap = ContextProjector().build(ctx)
    assert [t["content"] for t in snap.recent_turns] == ["好", "嗯嗯"]


def test_project_caps_max_turns(tmp_path):
    ctx = make_ctx(tmp_path)
    for i in range(30):
        ctx.add_user(f"t{i}")
    snap = ContextProjector(max_turns=5).build(ctx)
    assert len(snap.recent_turns) == 5
    assert snap.recent_turns[0]["content"] == "t29"  # 新→旧


def test_snapshot_is_frozen():
    snap = ContextSnapshot(recent_turns=({"content": "x", "kind": "user", "ts": 0.0},))
    with pytest.raises(Exception):
        snap.recent_turns = ()
