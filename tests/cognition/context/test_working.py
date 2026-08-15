import json
import time

from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_store(tmp_path):
    return ShortTermTurnStore(MemoryManager(MemoryStore(tmp_path / "m.db")))


def test_add_turns_and_situation(tmp_path):
    manager = make_store(tmp_path)
    ctx = WorkingContext(manager, snapshot_path=None)
    ctx.update_situation({"topic": "量子计算", "sensitive": False})
    ctx.add_user("你好")
    ctx.add_agent("我在")
    assert ctx.situation()["topic"] == "量子计算"
    assert ctx.turn_count() == 2
    items = ctx.items()
    assert [it["content"] for it in items] == ["我在", "你好"]
    assert [it["kind"] for it in items] == ["agent", "user"]


def test_snapshot_restore_roundtrip(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.update_situation({"topic": "X", "sensitive": False})
    ctx.add_user("第一轮")
    ctx.add_agent("回复")
    ctx.close()  # flush

    fresh = WorkingContext(make_store(tmp_path), snapshot_path=path, ttl_s=1800.0)
    fresh.restore()
    assert fresh.turn_count() == 2
    assert fresh.situation()["topic"] == "X"
    assert [it["content"] for it in fresh.items()] == ["回复", "第一轮"]


def test_restore_filters_expired_turns(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    old_ts = time.time() - 10000.0
    # 直接写快照模拟旧轮次
    path.write_text(json.dumps({
        "turns": [{"content": "旧轮", "kind": "user", "ts": old_ts},
                  {"content": "新轮", "kind": "user", "ts": time.time()}],
        "situation": None,
    }), encoding="utf-8")
    ctx.restore()
    contents = [it["content"] for it in ctx.items()]
    assert "旧轮" not in contents
    assert "新轮" in contents


def test_snapshot_path_none_does_not_write(tmp_path):
    manager = make_store(tmp_path)
    ctx = WorkingContext(manager, snapshot_path=None)
    ctx.add_user("x")
    ctx.close()
    assert not list(tmp_path.glob("*.json"))


def test_close_without_content_does_not_write_snapshot(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "empty_snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.close()
    assert not path.exists()


def test_snapshot_write_failure_warns(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "no_dir" / "snap.json"  # 父目录不存在 → snapshot 应自动创建
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.add_user("x")
    ctx.close()
    assert path.exists()
