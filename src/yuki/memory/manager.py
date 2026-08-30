from __future__ import annotations

import math
import time
from collections import deque
from pathlib import Path

from yuki.logger import get_logger
from yuki.memory.embedding import MemoryEmbeddingIndexer
from yuki.memory.store import StorageBackend

logger = get_logger("yuki.memory.manager")


class Reflector:
    """反思生成接口。无 LLM，本次不可用；LLM 接入后实现 generate 并落库为 reflection。"""

    def generate(self, scenario_ids: list[int], context: dict | None = None) -> list[str]:
        raise NotImplementedError("reflection generation requires an LLM (future)")


class ShortTermMemory:
    """短期（工作）记忆：进程内 TTL 队列，不落盘。"""

    def __init__(self, ttl_s: float = 1800, capacity: int = 50) -> None:
        self._ttl = ttl_s
        self._cap = capacity
        self._items: deque[dict] = deque()

    def add(self, content: str, *, kind: str = "event", at: float | None = None) -> None:
        self._items.append(
            {"content": content, "kind": kind, "ts": time.time() if at is None else at}
        )
        while len(self._items) > self._cap:
            self._items.popleft()

    def items(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        fresh = [it for it in self._items if now - it["ts"] <= self._ttl]
        self._items = deque(fresh, maxlen=self._cap)
        return list(reversed(fresh))

    def recent(self, n: int = 5, now: float | None = None) -> list[dict]:
        return self.items(now)[:n]

    def clear(self) -> None:
        self._items.clear()


class MemoryManager:
    """记忆门面：检索（FTS5 BM25 / 向量双路召回 + 向量重排）、清理策略、短期工作记忆。"""

    def __init__(
        self,
        store: StorageBackend,
        *,
        decay_base: float = 1.0,
        decay_lambda: float = 0.1,
        decay_threshold: float = 0.02,
        short_term_ttl_s: float = 1800,
        short_term_capacity: int = 50,
        short_term: ShortTermMemory | None = None,
        embedding_indexer: MemoryEmbeddingIndexer | None = None,
        vector_enabled: bool = False,
        vector_candidates: int = 30,
        superseded_retention_days: int = 30,
        tombstone_retention_days: int = 0,
    ) -> None:
        self._store = store
        self._base = decay_base
        self._lam = decay_lambda
        self._threshold = decay_threshold
        self._embedding_indexer = embedding_indexer
        self._vector_enabled = vector_enabled
        self._vector_candidates = vector_candidates
        self._superseded_retention_days = max(0, int(superseded_retention_days))
        self._tombstone_retention_days = max(0, int(tombstone_retention_days))
        self._short_term = short_term or ShortTermMemory(
            ttl_s=short_term_ttl_s, capacity=short_term_capacity,
        )

    @property
    def db_path(self) -> Path | None:
        return self._store.db_path

    def write(
        self,
        memory_type: str,
        content: str,
        *,
        confidence: float = 0.5,
        sensitivity: int = 0,
        source: str = "cli",
        metadata: dict | None = None,
    ) -> int:
        memory_id = self._store.create(
            memory_type, content,
            confidence=confidence, sensitivity=sensitivity,
            source=source, metadata=metadata,
        )
        if self._vector_enabled and self._embedding_indexer is not None:
            memory = self._store.get(memory_id)
            if memory is not None:
                try:
                    self._embedding_indexer.upsert(memory)
                except Exception:
                    logger.warning(
                        "memory embedding upsert failed",
                        memory_id=memory_id,
                        exc_info=True,
                    )
        return memory_id

    def get(self, memory_id: int) -> dict | None:
        mem = self._store.get(memory_id)
        if mem is not None:
            self._store.touch(memory_id)
        return mem

    def touch(self, memory_id: int) -> None:
        self._store.touch(memory_id)

    def delete(self, memory_id: int) -> bool:
        return self._store.delete(memory_id)

    def list(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> list[dict]:
        return self._store.list(memory_type=memory_type, min_sensitivity=min_sensitivity)

    def query(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
        touch: bool = True,
    ) -> list[dict]:
        text = (text or "").strip()
        if not text:
            return []
        if not self._vector_enabled or self._embedding_indexer is None:
            return self._query_lexical(
                text,
                memory_type=memory_type,
                top_k=top_k,
                min_sensitivity=min_sensitivity,
                touch=touch,
            )
        try:
            return self._query_hybrid(
                text,
                memory_type=memory_type,
                top_k=top_k,
                min_sensitivity=min_sensitivity,
                touch=touch,
            )
        except Exception:
            logger.warning("vector memory query failed, falling back to lexical", exc_info=True)
            return self._query_lexical(
                text,
                memory_type=memory_type,
                top_k=top_k,
                min_sensitivity=min_sensitivity,
                touch=touch,
            )

    def _query_lexical(
        self,
        text: str,
        *,
        memory_type: str | None,
        top_k: int,
        min_sensitivity: int,
        touch: bool,
    ) -> list[dict]:
        now = time.time()
        hits = self._store.query(
            text, memory_type=memory_type, top_k=top_k * 3, min_sensitivity=min_sensitivity,
        )
        scored: list[dict] = []
        for mem in hits:
            mem["score"] = self.decay_weight(mem, now)
            scored.append(mem)
        scored.sort(key=lambda m: m["score"], reverse=True)
        returned = scored[:top_k]
        if touch:
            for mem in returned:
                self._store.touch(mem["id"])
        return returned

    def _query_hybrid(
        self,
        text: str,
        *,
        memory_type: str | None,
        top_k: int,
        min_sensitivity: int,
        touch: bool,
    ) -> list[dict]:
        """BM25（FTS5）与向量双路召回，合并后统一按向量相似度 × 衰减重排，取 top_k。

        BM25 只负责召回候选、不参与打分；BM25 候选若有当前 embedding，也计算向量分。
        没有当前 embedding 的候选 vector_score=0，排在所有向量命中之后。
        """
        now = time.time()
        candidate_k = max(int(self._vector_candidates), int(top_k) * 3)
        lexical_hits = self._store.query(
            text, memory_type=memory_type, top_k=candidate_k, min_sensitivity=min_sensitivity,
        )
        vector_hits = self._embedding_indexer.search(
            text,
            memory_type=memory_type,
            top_k=candidate_k,
            min_sensitivity=min_sensitivity,
            include_ids=(mem["id"] for mem in lexical_hits),
        )
        by_id: dict[int, dict] = {}
        vector_scores: dict[int, float] = {}
        for mem in lexical_hits:
            by_id[mem["id"]] = mem
        for mem, score in vector_hits:
            by_id.setdefault(mem["id"], mem)
            vector_scores[mem["id"]] = max(0.0, min(float(score), 1.0))

        scored: list[dict] = []
        for memory_id, mem in by_id.items():
            vector_score = vector_scores.get(memory_id, 0.0)
            mem["vector_score"] = vector_score
            mem["score"] = vector_score * self.decay_weight(mem, now)
            scored.append(mem)
        scored.sort(
            key=lambda m: (m["score"], m["id"] in vector_scores),
            reverse=True,
        )
        returned = scored[:top_k]
        if touch:
            for mem in returned:
                self._store.touch(mem["id"])
        return returned

    def rebuild_embeddings(
        self,
        *,
        memory_type: str | None = None,
        min_sensitivity: int = 0,
    ) -> int:
        if self._embedding_indexer is None:
            return 0
        return self._embedding_indexer.rebuild(
            memory_type=memory_type,
            min_sensitivity=min_sensitivity,
        )

    def process_embedding_outbox(self, *, limit: int = 20) -> int:
        processed = 0
        for item in self._store.embedding_outbox(limit=limit):
            memory_id = int(item["memory_id"])
            operation = str(item["operation"])
            try:
                if operation == "upsert":
                    memory = self._store.get(memory_id)
                    if (
                        memory is not None
                        and self._vector_enabled
                        and self._embedding_indexer is not None
                    ):
                        self._embedding_indexer.upsert(memory)
                elif self._embedding_indexer is not None:
                    self._embedding_indexer.delete(memory_id)
                else:
                    self._store.delete_embeddings(memory_id)
                acknowledged = self._store.acknowledge_embedding_outbox(
                    memory_id,
                    operation,
                    float(item["queued_at"]),
                )
                processed += int(acknowledged)
            except Exception:
                logger.warning(
                    "memory embedding outbox item failed",
                    memory_id=memory_id,
                    operation=operation,
                    exc_info=True,
                )
        return processed

    def decay_weight(self, memory: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if memory["strengthened"]:
            return 1.0
        days = max(0.0, (now - memory["last_access"]) / 86400.0)
        return self._base * math.exp(-self._lam * days)

    def strengthen(self, memory_id: int) -> bool:
        return self._store.strengthen(memory_id)

    def cleanup(self) -> int:
        now = time.time()
        deleted = self._cleanup_decayed(now)
        inactive_deleted = self._store.cleanup_inactive(
            now=now,
            superseded_retention_days=self._superseded_retention_days,
            tombstone_retention_days=self._tombstone_retention_days,
        )
        return deleted + int(inactive_deleted or 0)

    def _cleanup_decayed(self, now: float) -> int:
        if self._threshold <= 0:
            return 0
        if self._base <= 0:
            return self._store.delete_decayed()
        if self._lam <= 0:
            return self._store.delete_decayed() if self._base < self._threshold else 0
        if self._threshold > self._base:
            return self._store.delete_decayed()

        stale_days = math.log(self._base / self._threshold) / self._lam
        return self._store.delete_decayed(last_access_before=now - stale_days * 86400.0)

    def wipe(self) -> int:
        return self._store.wipe()

    def ping(self) -> bool:
        return self._store.ping()

    def short_term_add(self, content: str, *, kind: str = "event", at: float | None = None) -> None:
        self._short_term.add(content, kind=kind, at=at)

    def short_term_items(self) -> list[dict]:
        return self._short_term.items()

    def short_term_clear(self) -> None:
        self._short_term.clear()

    def close(self) -> None:
        self._store.close()
