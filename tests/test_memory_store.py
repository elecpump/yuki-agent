import sqlite3

import pytest

from yuki.memory.embedding import encode_vector
from yuki.memory.store import MemoryError, MemoryStore, StorageBackend


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


def test_delete_cascades_embeddings(store):
    mem_id = store.create("preference", "vector memory")
    store.upsert_embedding(
        mem_id,
        provider="hashing",
        model="hashing-v1",
        dimension=2,
        embedding=encode_vector([1.0, 0.0]),
        content_hash="hash1",
    )

    assert store.embeddings_count() == 1
    assert store.delete(mem_id) is True
    assert store.embeddings_count() == 0


def test_wipe_clears_embeddings(store):
    mem_id = store.create("preference", "vector memory")
    store.upsert_embedding(
        mem_id,
        provider="hashing",
        model="hashing-v1",
        dimension=2,
        embedding=encode_vector([1.0, 0.0]),
        content_hash="hash1",
    )

    assert store.wipe() == 1
    assert store.embeddings_count() == 0


def test_embeddings_are_keyed_by_provider_model_and_dimension(store):
    mem_id = store.create("preference", "vector memory")
    store.upsert_embedding(
        mem_id,
        provider="hashing",
        model="hashing-v1",
        dimension=2,
        embedding=encode_vector([1.0, 0.0]),
        content_hash="hash1",
    )
    store.upsert_embedding(
        mem_id,
        provider="hashing",
        model="hashing-v2",
        dimension=2,
        embedding=encode_vector([0.0, 1.0]),
        content_hash="hash1",
    )

    assert store.embeddings_count() == 2
    v1_rows = store.vector_rows(provider="hashing", model="hashing-v1", dimension=2)
    v2_rows = store.vector_rows(provider="hashing", model="hashing-v2", dimension=2)
    assert len(v1_rows) == 1
    assert len(v2_rows) == 1
    assert "embedding" not in v1_rows[0][0]


def test_normal_retrieval_excludes_superseded_memory_and_vectors(store):
    active_id = store.create("preference", "active preference")
    superseded_id = store.create("preference", "superseded preference")
    for memory_id in (active_id, superseded_id):
        store.upsert_embedding(
            memory_id,
            provider="hashing",
            model="hashing-v1",
            dimension=2,
            embedding=encode_vector([1.0, 0.0]),
            content_hash=str(memory_id),
        )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE memories SET state = 'superseded' WHERE id = ?",
            (superseded_id,),
        )

    assert [memory["id"] for memory in store.list()] == [active_id]
    assert store.search("superseded") == []
    vector_ids = {
        memory["id"]
        for memory, _ in store.vector_rows(
            provider="hashing",
            model="hashing-v1",
            dimension=2,
        )
    }
    assert vector_ids == {active_id}


def test_opening_legacy_database_adds_memory_lifecycle_columns(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                sensitivity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'cli',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                last_access REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                strengthened INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories (
                memory_type, content, created_at, last_access
            ) VALUES ('preference', 'legacy memory', 123.0, 123.0)
            """
        )

    store = MemoryStore(path)
    try:
        memory = store.get(1)
        assert memory["state"] == "active"
        assert memory["revision"] == 1
        assert memory["updated_at"] == 123.0
        assert memory["supersedes_id"] is None
    finally:
        store.close()


def test_delete_decayed_removes_only_eligible_old_memories(store):
    stale = store.create("scenario", "stale quantum note")
    fresh = store.create("scenario", "fresh quantum note")
    personal = store.create("personal", "personal quantum note")
    strong = store.create("preference", "strong quantum note")

    store.touch(stale, at=100.0)
    store.touch(personal, at=100.0)
    store.touch(strong, at=100.0)
    store.strengthen(strong)

    assert store.delete_decayed(last_access_before=200.0) == 1
    remaining = {m["id"] for m in store.all()}
    assert stale not in remaining
    assert fresh in remaining
    assert personal in remaining
    assert strong in remaining
    assert stale not in [hit[0]["id"] for hit in store.search("quantum")]


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


def test_fts_sync_delete_then_search(store):
    a = store.create("preference", "quantum computing rocks")
    b = store.create("preference", "quantum entanglement theory")
    assert len(store.search("quantum")) == 2
    store.delete(a)
    hits = store.search("quantum")
    assert [h[0]["id"] for h in hits] == [b]


def test_fts_sync_wipe_then_search(store):
    store.create("preference", "quantum computing rocks")
    assert store.search("quantum") != []
    store.wipe()
    assert store.search("quantum") == []


def test_fts_sync_update_then_search(store):
    mem_id = store.create("preference", "quantum computing rocks")
    store._conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("classical physics theory", mem_id),
    )
    store._conn.commit()
    assert store.search("quantum") == []
    hits = store.search("classical")
    assert len(hits) == 1
    assert hits[0][0]["content"] == "classical physics theory"


def test_search_fts_filters_and_limits(store):
    for i in range(6):
        store.create("preference", f"quantum topic number {i}", sensitivity=0)
    store.create("scenario", "quantum sensitive topic", sensitivity=2)
    hits = store.search("quantum", top_k=3)
    assert len(hits) == 3
    assert all(rank < 1.0 for _, rank in hits)
    assert all(m["memory_type"] == "preference" for m, _ in hits)
    typed = store.search("quantum", memory_type="scenario")
    assert len(typed) == 1
    assert typed[0][0]["sensitivity"] == 2
    low = store.search("quantum", min_sensitivity=2)
    assert len(low) == 1
    assert low[0][0]["sensitivity"] == 2
    assert low[0][1] < 1.0


def test_search_like_fallback_escapes_wildcards(store):
    store.create("preference", "50% off sale")
    store.create("preference", "plain_text notes")
    hits = store.search("%")
    assert [h[0]["content"] for h in hits] == ["50% off sale"]
    hits = store.search("_")
    assert [h[0]["content"] for h in hits] == ["plain_text notes"]
    assert store.search("50%x") == []


def test_corrupt_metadata_raises_memory_error(store):
    mem_id = store.create("preference", "a")
    store._conn.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?", ("{not json", mem_id),
    )
    store._conn.commit()
    with pytest.raises(MemoryError):
        store.list()
    with pytest.raises(MemoryError):
        store.all()
    with pytest.raises(MemoryError):
        store.search("a")


def test_create_rejects_out_of_range_sensitivity(store):
    with pytest.raises(MemoryError):
        store.create("preference", "x", sensitivity=3)
    with pytest.raises(MemoryError):
        store.create("preference", "x", sensitivity=-1)


def test_create_rejects_out_of_range_confidence(store):
    with pytest.raises(MemoryError):
        store.create("preference", "x", confidence=1.5)
    with pytest.raises(MemoryError):
        store.create("preference", "x", confidence=-0.1)


def test_memory_store_satisfies_storage_backend_protocol(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    try:
        assert isinstance(store, StorageBackend)
    finally:
        store.close()


def test_storage_backend_protocol_has_minimal_surface():
    assert hasattr(StorageBackend, "persist")
    assert hasattr(StorageBackend, "query")
    assert hasattr(StorageBackend, "vacuum")


def test_memory_create_writes_audit(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.memory.store.get_audit_logger", lambda: FakeAudit())
    store = MemoryStore(tmp_path / "m.db")
    try:
        memory_id = store.create("personal", "content", source="cli")
        assert calls[0][0] == "memory.create"
        assert calls[0][1]["memory_id"] == memory_id
        assert calls[0][1]["memory_type"] == "personal"
    finally:
        store.close()
