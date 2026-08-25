from yuki.model_cache import ModelCacheManager


def test_model_cache_manager_evicts_lru_entries():
    cache = ModelCacheManager(max_entries=2)

    cache.put("vlm", "a", 1)
    cache.put("vlm", "b", 2)
    assert cache.get("vlm", "a") == 1
    cache.put("vlm", "c", 3)

    assert cache.get("vlm", "a") == 1
    assert cache.get("vlm", "b") is None
    assert cache.get("vlm", "c") == 3


def test_model_cache_manager_expires_entries():
    now = [0.0]
    cache = ModelCacheManager(max_entries=2, clock=lambda: now[0])

    cache.put("vlm", "a", 1, ttl_s=10.0)
    assert cache.get("vlm", "a") == 1
    now[0] = 10.0

    assert cache.get("vlm", "a") is None
    assert cache.stats()["entries"] == 0


def test_model_cache_manager_clears_one_namespace():
    cache = ModelCacheManager(max_entries=4)
    cache.put("vlm", "a", 1)
    cache.put("memory", "a", 2)

    assert cache.clear("vlm") == 1

    assert cache.get("vlm", "a") is None
    assert cache.get("memory", "a") == 2
