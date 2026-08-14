import pytest

from yuki.memory.store import MemoryError, MemoryStore


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def test_create_and_get(store):
    mem_id = store.create("preference", "用户喜欢安静的环境")
    mem = store.get(mem_id)
    assert mem["id"] == mem_id
    assert mem["memory_type"] == "preference"
    assert mem["content"] == "用户喜欢安静的环境"
    assert mem["confidence"] == 0.5
    assert mem["sensitivity"] == 0
    assert mem["source"] == "cli"
    assert mem["metadata"] == {}
    assert mem["strengthened"] is False
    assert mem["last_access"] == mem["created_at"]


def test_create_rejects_unknown_type(store):
    with pytest.raises(MemoryError):
        store.create("unknown", "x")


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_delete_returns_rowcount(store):
    mem_id = store.create("preference", "a")
    assert store.delete(mem_id) is True
    assert store.delete(mem_id) is False


def test_list_filters_type_and_sensitivity(store):
    store.create("preference", "喜欢茶", sensitivity=0)
    store.create("preference", "喜欢咖啡", sensitivity=1)
    store.create("scenario", "在读书", sensitivity=0)
    prefs = store.list(memory_type="preference")
    assert len(prefs) == 2
    only_high = store.list(min_sensitivity=1)
    assert [m["content"] for m in only_high] == ["喜欢咖啡"]


def test_search_cjk_two_char_via_like_fallback(store):
    store.create("preference", "用户喜欢量子计算")
    store.create("preference", "用户在研究股票")
    hits = store.search("计算")
    assert len(hits) == 1
    assert hits[0][0]["content"] == "用户喜欢量子计算"


def test_search_english_substring_via_fts(store):
    store.create("scenario", "user likes quantum computing")
    store.create("scenario", "user likes cooking")
    hits = store.search("quant")
    assert len(hits) == 1
    assert hits[0][0]["content"] == "user likes quantum computing"


def test_search_filters_and_limits(store):
    for i in range(6):
        store.create("preference", f"喜欢话题{i}")
    hits = store.search("话题", top_k=3, memory_type="preference")
    assert len(hits) == 3


def test_search_empty_text_returns_empty(store):
    store.create("preference", "a")
    assert store.search("") == []
    assert store.search("   ") == []


def test_touch_updates_last_access_and_count(store):
    mem_id = store.create("preference", "a")
    before = store.get(mem_id)
    store.touch(mem_id, at=before["created_at"] + 86400.0)
    after = store.get(mem_id)
    assert after["last_access"] > before["last_access"]
    assert after["access_count"] == 1


def test_strengthen_marks_and_resets_last_access(store):
    mem_id = store.create("preference", "a")
    old = store.get(mem_id)
    store.touch(mem_id, at=old["created_at"] - 86400.0)
    assert store.strengthen(mem_id) is True
    mem = store.get(mem_id)
    assert mem["strengthened"] is True
    assert mem["last_access"] > old["created_at"]
    assert store.strengthen(999) is False


def test_wipe_clears_all(store):
    store.create("preference", "a")
    store.create("scenario", "b")
    assert store.wipe() == 2
    assert store.all() == []


def test_ping_true_for_valid_db(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    assert s.ping() is True
    s.close()
