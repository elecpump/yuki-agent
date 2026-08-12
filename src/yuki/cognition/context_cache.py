import collections
from typing import Any


class ContextCache:
    """VLM 情境缓存：LRU，键 = 窗口标题|URL域|滚动位置%。"""

    def __init__(self, max_entries: int = 64) -> None:
        self._max = max_entries
        self._store: collections.OrderedDict[str, dict] = collections.OrderedDict()

    def get(self, key: str) -> dict | None:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: str, value: dict) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)
