from yuki.model_cache import ModelCacheManager


class ContextCache:
    """VLM 情境缓存：LRU，键 = 窗口标题|URL域|滚动位置%。"""

    def __init__(
        self,
        max_entries: int = 64,
        *,
        cache_manager: ModelCacheManager | None = None,
        namespace: str = "vlm_context",
        ttl_s: float | None = None,
    ) -> None:
        self._manager = cache_manager or ModelCacheManager(max_entries=max_entries)
        self._namespace = namespace
        self._ttl_s = ttl_s

    def get(self, key: str) -> dict | None:
        value = self._manager.get(self._namespace, key)
        return value if isinstance(value, dict) else None

    def put(self, key: str, value: dict) -> None:
        self._manager.put(self._namespace, key, value, ttl_s=self._ttl_s)

    def clear(self) -> int:
        return self._manager.clear(self._namespace)

    def stats(self) -> dict:
        return self._manager.stats()
