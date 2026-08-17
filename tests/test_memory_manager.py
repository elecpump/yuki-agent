import time

import pytest

from yuki.memory.manager import MemoryManager, Reflector, ShortTermMemory
from yuki.memory.store import MemoryStore


@pytest.fixture()
def manager(tmp_path):
    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        decay_base=1.0, decay_lambda=1.0, decay_threshold=0.3,
    )
    yield m
    m.close()


def test_write_returns_id_and_query_ranks_freshness(manager):
    old_id = manager.write("preference", "旧记忆", source="cli")
    manager._store.touch(old_id, at=1000000.0)  # 10 天前
    fresh_id = manager.write("preference", "新鲜记忆", source="cli")
    results = manager.query("记忆", top_k=5)
    assert results[0]["id"] == fresh_id


def test_query_returns_scores_and_touches(manager):
    mem_id = manager.write("preference", "喜欢咖啡")
    manager._store.touch(mem_id, at=time.time() - 3 * 86400)
    results = manager.query("咖啡")
    assert results[0]["score"] > 0.0
    assert manager._store.get(mem_id)["access_count"] == 2


def test_query_only_touches_returned_results(manager):
    first = manager.write("preference", "xy old")
    second = manager.write("preference", "xy middle")

    results = manager.query("xy", top_k=1)
    returned = results[0]["id"]
    not_returned = {first, second} - {returned}

    assert manager._store.get(returned)["access_count"] == 1
    for memory_id in not_returned:
        assert manager._store.get(memory_id)["access_count"] == 0


def test_decay_weight_strengthened_is_one(manager):
    mem_id = manager.write("preference", "x")
    manager.strengthen(mem_id)
    mem = manager.get(mem_id)
    assert manager.decay_weight(mem, now=2000000000.0) == 1.0


def test_decay_weight_decays_over_time(manager):
    mem_id = manager.write("preference", "x")
    mem = manager.get(mem_id)
    fresh = manager.decay_weight(mem, now=mem["created_at"] + 86400.0)
    old = manager.decay_weight(mem, now=mem["created_at"] + 86400.0 * 10)
    assert fresh > old


def test_cleanup_removes_stale_but_keeps_personal_and_strengthened(manager):
    stale = manager.write("scenario", "旧场景")
    manager._store.touch(stale, at=1000000.0)
    personal = manager.write("personal", "我的名字")
    manager._store.touch(personal, at=1000000.0)
    strong = manager.write("preference", "强化项")
    manager.strengthen(strong)
    manager._store.touch(strong, at=1000000.0)
    deleted = manager.cleanup()
    assert deleted == 1
    ids = [m["id"] for m in manager.list()]
    assert stale not in ids
    assert personal in ids
    assert strong in ids


def test_cleanup_does_not_load_all_memories(tmp_path):
    class NoFullScanStore(MemoryStore):
        def all(self):
            raise AssertionError("cleanup should delete stale memories in SQL")

    m = MemoryManager(
        NoFullScanStore(tmp_path / "mem.db"),
        decay_base=1.0,
        decay_lambda=1.0,
        decay_threshold=0.3,
    )
    try:
        stale = m.write("scenario", "stale")
        m._store.touch(stale, at=1000000.0)

        assert m.cleanup() == 1
        assert m.get(stale) is None
    finally:
        m.close()


def test_wipe_and_ping(manager):
    manager.write("preference", "a")
    assert manager.ping() is True
    assert manager.wipe() == 1


def test_short_term_ttl_evicts_expired():
    st = ShortTermMemory(ttl_s=10, capacity=3)
    st.add("a", at=100.0)
    st.add("b", at=200.0)
    assert [it["content"] for it in st.items(now=205.0)] == ["b"]
    assert [it["content"] for it in st.items(now=215.0)] == []


def test_short_term_capacity_evicts_oldest():
    st = ShortTermMemory(ttl_s=100, capacity=3)
    for i in range(4):
        st.add(f"item{i}", at=float(i))
    assert [it["content"] for it in st.items(now=50.0)] == ["item3", "item2", "item1"]


def test_reflector_generate_not_implemented():
    with pytest.raises(NotImplementedError):
        Reflector().generate([1, 2])


def test_manager_short_term_capacity_param_honored(tmp_path):
    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        short_term_capacity=2,
    )
    m.short_term_add("a")
    m.short_term_add("b")
    m.short_term_add("c")
    assert len(m.short_term_items()) == 2
    m.close()


def test_manager_short_term_ttl_param_honored(tmp_path):
    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        short_term_ttl_s=-1,
        short_term_capacity=10,
    )
    m.short_term_add("a")
    assert m.short_term_items() == []
    m.close()


def test_short_term_add_with_at_and_clear():
    manager = MemoryManager(MemoryStore(":memory:"))
    t0 = time.time()
    manager.short_term_add("a", kind="turn", at=t0)
    manager.short_term_add("b", kind="turn", at=t0 + 100.0)
    items = manager.short_term_items()
    assert [it["content"] for it in items] == ["b", "a"]
    assert items[1]["ts"] == t0
    manager.short_term_clear()
    assert manager.short_term_items() == []
