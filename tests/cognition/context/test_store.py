import time

from yuki.cognition.context.store import ShortTermTurnStore
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_short_term_turn_store_add_items_clear(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    store = ShortTermTurnStore(manager)
    t0 = time.time()
    store.add("你好", "user", t0)
    store.add("我在", "agent", t0 + 100.0)
    items = store.items()
    assert [it["content"] for it in items] == ["我在", "你好"]
    assert items[0]["kind"] == "agent"
    store.clear()
    assert store.items() == []
