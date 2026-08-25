import time

import numpy as np
import pytest

from yuki.memory.embedding import (
    decode_vector,
    EmbeddingProviderRegistry,
    HashingEmbeddingProvider,
    MemoryEmbeddingIndexer,
    build_embedding_indexer,
)
from yuki.memory.manager import MemoryManager, Reflector, ShortTermMemory
from yuki.memory.store import MemoryStore
from yuki.model_cache import ModelCacheManager


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


def test_vector_disabled_preserves_lexical_query_shape(tmp_path):
    class RaisingIndexer:
        def upsert(self, memory):
            raise AssertionError("disabled vector path should not index writes")

        def search(self, text, *, top_k, memory_type=None, min_sensitivity=0):
            raise AssertionError("disabled vector path should not search")

    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        embedding_indexer=RaisingIndexer(),
        vector_enabled=False,
    )
    try:
        m.write("preference", "needle memory", confidence=0.95)
        results = m.query("needle", top_k=1)
        assert results[0]["content"] == "needle memory"
        assert "lexical_score" not in results[0]
        assert "vector_score" not in results[0]
    finally:
        m.close()


def test_vector_query_can_return_non_lexical_hit(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    indexer = MemoryEmbeddingIndexer(store, HashingEmbeddingProvider(dimension=64))
    m = MemoryManager(
        store,
        embedding_indexer=indexer,
        vector_enabled=True,
        lexical_weight=0.0,
        vector_weight=1.0,
        confidence_weight=0.0,
    )
    try:
        mem_id = m.write("preference", "saffron noodle preference")
        assert store.search("sfron") == []

        results = m.query("sfron", top_k=1)
        assert results[0]["id"] == mem_id
        assert results[0]["vector_score"] > 0.0
    finally:
        m.close()


def test_embedding_upsert_stores_normalized_vector(tmp_path):
    class FixedProvider:
        name = "fixed"
        model = "fixed-v1"
        dimension = 2

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[3.0, 4.0] for _ in texts]

    store = MemoryStore(tmp_path / "mem.db")
    indexer = MemoryEmbeddingIndexer(store, FixedProvider())
    try:
        mem_id = store.create("preference", "fixed vector")
        assert indexer.upsert(store.get(mem_id)) is True

        rows = store.vector_rows(provider="fixed", model="fixed-v1", dimension=2)
        vector = decode_vector(rows[0][1])
        assert np.allclose(vector, [0.6, 0.8])
    finally:
        store.close()


def test_vector_search_reuses_cached_matrix_and_reads_fresh_memory(tmp_path):
    class CountingStore(MemoryStore):
        def __init__(self, db_path):
            super().__init__(db_path)
            self.vector_rows_calls = 0

        def vector_rows(self, **kwargs):
            self.vector_rows_calls += 1
            return super().vector_rows(**kwargs)

    store = CountingStore(tmp_path / "mem.db")
    indexer = MemoryEmbeddingIndexer(store, HashingEmbeddingProvider(dimension=32))
    try:
        mem_id = store.create("preference", "alpha memory")
        indexer.upsert(store.get(mem_id))

        assert indexer.search("alpha", top_k=1)[0][0]["access_count"] == 0
        store.touch(mem_id, at=123456.0)
        assert indexer.search("alpha", top_k=1)[0][0]["access_count"] == 1
        assert store.vector_rows_calls == 1
    finally:
        store.close()


def test_vector_search_can_use_shared_model_cache_manager(tmp_path):
    class CountingStore(MemoryStore):
        def __init__(self, db_path):
            super().__init__(db_path)
            self.vector_rows_calls = 0

        def vector_rows(self, **kwargs):
            self.vector_rows_calls += 1
            return super().vector_rows(**kwargs)

    store = CountingStore(tmp_path / "mem.db")
    cache_manager = ModelCacheManager(max_entries=10)
    indexer = MemoryEmbeddingIndexer(
        store,
        HashingEmbeddingProvider(dimension=32),
        cache_manager=cache_manager,
    )
    try:
        mem_id = store.create("preference", "alpha memory")
        indexer.upsert(store.get(mem_id))

        assert indexer.search("alpha", top_k=1)
        assert indexer.search("alpha", top_k=1)
        assert store.vector_rows_calls == 1
        assert cache_manager.stats()["namespaces"]["memory_vector_matrix"] == 1
    finally:
        store.close()


def test_vector_cache_refreshes_after_delete(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    indexer = MemoryEmbeddingIndexer(store, HashingEmbeddingProvider(dimension=32))
    try:
        first = store.create("preference", "alpha first")
        second = store.create("preference", "alpha second")
        indexer.upsert(store.get(first))
        indexer.upsert(store.get(second))

        assert indexer.search("alpha", top_k=2)
        assert store.delete(first) is True

        ids = [memory["id"] for memory, _ in indexer.search("alpha", top_k=2)]
        assert first not in ids
        assert second in ids
    finally:
        store.close()


def test_vector_candidates_scale_with_top_k(tmp_path):
    class RecordingIndexer:
        def __init__(self):
            self.top_k = None

        def search(self, text, *, top_k, memory_type=None, min_sensitivity=0):
            self.top_k = top_k
            return []

    indexer = RecordingIndexer()
    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        embedding_indexer=indexer,
        vector_enabled=True,
        vector_candidates=2,
    )
    try:
        assert m.query("missing", top_k=5) == []
        assert indexer.top_k == 15
    finally:
        m.close()


def test_vector_query_failure_falls_back_to_lexical(tmp_path):
    class FailingIndexer:
        def search(self, text, *, top_k, memory_type=None, min_sensitivity=0):
            raise RuntimeError("embedding provider down")

    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        embedding_indexer=FailingIndexer(),
        vector_enabled=True,
    )
    try:
        m.write("preference", "fallback needle")
        results = m.query("needle", top_k=1)
        assert results[0]["content"] == "fallback needle"
        assert "vector_score" not in results[0]
    finally:
        m.close()


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


def test_embedding_registry_builds_hashing_by_default():
    registry = EmbeddingProviderRegistry()
    registry.register("hashing", lambda **kw: HashingEmbeddingProvider(**kw))
    provider = registry.build("hashing", dimension=64, model="m")
    assert provider.name == "hashing"
    assert provider.dimension == 64


def test_embedding_registry_raises_on_unknown():
    registry = EmbeddingProviderRegistry()
    with pytest.raises(ValueError, match="unknown embedding provider"):
        registry.build("nope")


def test_build_embedding_indexer_uses_registry(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    seen = {}

    def fake_factory(**kwargs):
        seen.update(kwargs)
        return HashingEmbeddingProvider(**kwargs)

    registry = EmbeddingProviderRegistry()
    registry.register("fake", fake_factory)
    indexer = build_embedding_indexer(
        store,
        provider_name="fake",
        dimension=48,
        model="fake-v1",
        registry=registry,
    )
    try:
        assert indexer.provider.dimension == 48
        assert seen["model"] == "fake-v1"
    finally:
        store.close()


def test_memory_manager_accepts_any_storage_backend():
    from yuki.memory.store import StorageBackend

    class FakeBackend(StorageBackend):
        def __init__(self):
            self.calls = []

        def persist(self):
            self.calls.append("persist")

        def query(self, text, *, memory_type=None, top_k=5, min_sensitivity=0):
            self.calls.append(("query", text, memory_type, top_k, min_sensitivity))
            return []

        def vacuum(self):
            self.calls.append("vacuum")

    backend = FakeBackend()
    manager = MemoryManager(backend)
    assert manager.query("hi", top_k=3) == []
    assert ("query", "hi", None, 9, 0) in backend.calls
