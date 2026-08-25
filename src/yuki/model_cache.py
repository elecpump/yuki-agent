from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _CacheEntry:
    value: Any
    weight: int
    expires_at: float | None


class ModelCacheManager:
    """Shared in-process cache manager for model-adjacent caches.

    Entries are evicted globally by LRU order. Namespaces keep unrelated caches
    easy to inspect or clear without forcing each caller to implement policies.
    """

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_weight: int = 0,
        default_ttl_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_weight = max(0, int(max_weight))
        self._default_ttl_s = default_ttl_s
        self._clock = clock
        self._store: OrderedDict[tuple[str, Any], _CacheEntry] = OrderedDict()
        self._total_weight = 0

    def get(self, namespace: str, key: Any) -> Any | None:
        compound = (namespace, key)
        entry = self._store.get(compound)
        if entry is None:
            return None
        if self._expired(entry):
            self._remove(compound)
            return None
        self._store.move_to_end(compound)
        return entry.value

    def put(
        self,
        namespace: str,
        key: Any,
        value: Any,
        *,
        weight: int = 1,
        ttl_s: float | None = None,
    ) -> None:
        compound = (namespace, key)
        if compound in self._store:
            self._remove(compound)
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        expires_at = None if ttl is None else self._clock() + max(0.0, float(ttl))
        entry = _CacheEntry(value=value, weight=max(1, int(weight)), expires_at=expires_at)
        self._store[compound] = entry
        self._total_weight += entry.weight
        self._evict()

    def clear(self, namespace: str | None = None) -> int:
        if namespace is None:
            count = len(self._store)
            self._store.clear()
            self._total_weight = 0
            return count
        keys = [key for key in self._store if key[0] == namespace]
        for key in keys:
            self._remove(key)
        return len(keys)

    def stats(self) -> dict:
        self._evict_expired()
        namespaces: dict[str, int] = {}
        for namespace, _ in self._store:
            namespaces[namespace] = namespaces.get(namespace, 0) + 1
        return {
            "entries": len(self._store),
            "max_entries": self._max_entries,
            "weight": self._total_weight,
            "max_weight": self._max_weight,
            "namespaces": namespaces,
        }

    def _expired(self, entry: _CacheEntry) -> bool:
        return entry.expires_at is not None and self._clock() >= entry.expires_at

    def _evict(self) -> None:
        self._evict_expired()
        while len(self._store) > self._max_entries:
            self._pop_oldest()
        while self._max_weight > 0 and self._total_weight > self._max_weight:
            self._pop_oldest()

    def _evict_expired(self) -> None:
        for key, entry in list(self._store.items()):
            if self._expired(entry):
                self._remove(key)

    def _pop_oldest(self) -> None:
        key, _ = next(iter(self._store.items()))
        self._remove(key)

    def _remove(self, key: tuple[str, Any]) -> None:
        entry = self._store.pop(key)
        self._total_weight -= entry.weight
