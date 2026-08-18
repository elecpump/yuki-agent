from __future__ import annotations

import hashlib
import math
import time
from typing import Protocol

import numpy as np

from yuki.logger import get_logger
from yuki.memory.store import MemoryStore

logger = get_logger("yuki.memory.embedding")


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """Zero-dependency feature-hashing provider for wiring and tests, not true semantics."""

    name = "hashing"

    def __init__(self, *, dimension: int = 384, model: str = "hashing-v1") -> None:
        self.dimension = int(dimension)
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text).tolist() for text in texts]

    def _embed_one(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype="<f4")
        text = (text or "").lower()
        features = self._features(text)
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[slot] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector

    def _features(self, text: str) -> list[str]:
        chars = [ch for ch in text if not ch.isspace()]
        features: list[str] = []
        features.extend(chars)
        features.extend("".join(chars[i : i + 2]) for i in range(max(0, len(chars) - 1)))
        features.extend("".join(chars[i : i + 3]) for i in range(max(0, len(chars) - 2)))
        return features


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def encode_vector(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0 or query.size == 0:
        return np.zeros((0,), dtype="<f4")
    query_norm = float(np.linalg.norm(query))
    row_norms = np.linalg.norm(matrix, axis=1)
    denom = row_norms * query_norm
    scores = np.zeros((matrix.shape[0],), dtype="<f4")
    valid = denom > 0
    if np.any(valid):
        scores[valid] = (matrix[valid] @ query) / denom[valid]
    return np.clip((scores + 1.0) / 2.0, 0.0, 1.0)


class MemoryEmbeddingIndexer:
    def __init__(self, store: MemoryStore, provider: EmbeddingProvider) -> None:
        self._store = store
        self.provider = provider

    @property
    def dimension(self) -> int:
        return int(self.provider.dimension)

    def upsert(self, memory: dict) -> bool:
        digest = content_hash(memory.get("content", ""))
        existing = self._store.embedding_metadata(
            memory["id"],
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
        )
        if existing is not None and existing.get("content_hash") == digest:
            return False
        vector = self.provider.embed([memory.get("content", "")])[0]
        self._store.upsert_embedding(
            memory["id"],
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
            embedding=encode_vector(vector),
            content_hash=digest,
            updated_at=time.time(),
        )
        return True

    def rebuild(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> int:
        count = 0
        for memory in self._store.list(memory_type=memory_type, min_sensitivity=min_sensitivity):
            if self.upsert(memory):
                count += 1
        return count

    def search(
        self,
        text: str,
        *,
        top_k: int,
        memory_type: str | None = None,
        min_sensitivity: int = 0,
    ) -> list[tuple[dict, float]]:
        query_vector = np.asarray(self.provider.embed([text])[0], dtype="<f4")
        rows = self._store.vector_rows(
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
            memory_type=memory_type,
            min_sensitivity=min_sensitivity,
        )
        if not rows:
            return []
        memories = [memory for memory, _ in rows]
        matrix = np.vstack([decode_vector(blob) for _, blob in rows]).astype("<f4", copy=False)
        scores = cosine_scores(query_vector, matrix)
        order = np.argsort(scores)[::-1][: max(0, int(top_k))]
        return [
            (memories[int(i)], float(scores[int(i)]))
            for i in order
            if not math.isnan(float(scores[int(i)]))
        ]


def build_embedding_indexer(
    store: MemoryStore,
    *,
    provider_name: str = "hashing",
    model: str = "hashing-v1",
    dimension: int = 384,
) -> MemoryEmbeddingIndexer:
    if provider_name != "hashing":
        raise ValueError(f"unknown embedding provider: {provider_name}")
    return MemoryEmbeddingIndexer(
        store,
        HashingEmbeddingProvider(dimension=dimension, model=model),
    )
