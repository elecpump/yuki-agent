from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from yuki.logger import get_logger
from yuki.memory.store import MemoryStore

logger = get_logger("yuki.memory.embedding")


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingProviderRegistry:
    """Build embedding providers by name."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., EmbeddingProvider]] = {}

    def register(self, name: str, factory: Callable[..., EmbeddingProvider]) -> None:
        self._factories[name] = factory

    def build(self, name: str, **kwargs) -> EmbeddingProvider:
        factory = self._factories.get(name)
        if factory is None:
            raise ValueError(f"unknown embedding provider: {name}")
        return factory(**kwargs)


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


default_embedding_registry = EmbeddingProviderRegistry()
default_embedding_registry.register(
    "hashing",
    lambda **kwargs: HashingEmbeddingProvider(**kwargs),
)


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def encode_vector(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def normalize_vector(vector: list[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype="<f4")
    norm = float(np.linalg.norm(array))
    if norm <= 0:
        return array.astype("<f4", copy=False)
    return (array / norm).astype("<f4", copy=False)


def normalized_cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0 or query.size == 0:
        return np.zeros((0,), dtype="<f4")
    return np.clip((matrix @ query + 1.0) / 2.0, 0.0, 1.0).astype("<f4", copy=False)


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0 or query.size == 0:
        return np.zeros((0,), dtype="<f4")
    row_norms = np.linalg.norm(matrix, axis=1)
    normalized = matrix.astype("<f4", copy=True)
    valid = row_norms > 0
    if np.any(valid):
        normalized[valid] /= row_norms[valid, None]
    return normalized_cosine_scores(normalize_vector(query), normalized)


def top_k_indices(scores: np.ndarray, top_k: int) -> np.ndarray:
    limit = max(0, int(top_k))
    if limit == 0 or scores.size == 0:
        return np.asarray([], dtype=np.intp)
    safe_scores = np.nan_to_num(scores, nan=-np.inf)
    limit = min(limit, scores.size)
    if limit == scores.size:
        return np.argsort(safe_scores)[::-1]
    candidates = np.argpartition(safe_scores, -limit)[-limit:]
    return candidates[np.argsort(safe_scores[candidates])[::-1]]


@dataclass(frozen=True)
class _VectorMatrixCache:
    signature: tuple[int, float | None]
    memory_ids: tuple[int, ...]
    matrix: np.ndarray


def _build_normalized_matrix(rows: list[tuple[dict, bytes]]) -> tuple[tuple[int, ...], np.ndarray]:
    if not rows:
        return (), np.zeros((0, 0), dtype="<f4")
    memory_ids = tuple(int(memory["id"]) for memory, _ in rows)
    matrix = np.vstack([decode_vector(blob) for _, blob in rows]).astype("<f4", copy=False)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    matrix = matrix / norms
    return memory_ids, matrix


def _cache_key(
    memory_type: str | None,
    min_sensitivity: int,
    provider: str,
    model: str,
    dimension: int,
) -> tuple[str, str, int, str | None, int]:
    return provider, model, int(dimension), memory_type, int(min_sensitivity)


class MemoryEmbeddingIndexer:
    def __init__(self, store: MemoryStore, provider: EmbeddingProvider) -> None:
        self._store = store
        self.provider = provider
        self._matrix_cache: dict[
            tuple[str, str, int, str | None, int],
            _VectorMatrixCache,
        ] = {}

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
        vector = normalize_vector(self.provider.embed([memory.get("content", "")])[0])
        self._store.upsert_embedding(
            memory["id"],
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
            embedding=encode_vector(vector),
            content_hash=digest,
            updated_at=time.time(),
        )
        self._matrix_cache.clear()
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
        limit = max(0, int(top_k))
        if limit == 0:
            return []
        query_vector = normalize_vector(self.provider.embed([text])[0])
        cache = self._cached_matrix(
            memory_type=memory_type,
            min_sensitivity=min_sensitivity,
        )
        if cache.matrix.size == 0:
            return []
        scores = normalized_cosine_scores(query_vector, cache.matrix)
        order = top_k_indices(scores, limit)
        results: list[tuple[dict, float]] = []
        for index in order:
            score = float(scores[int(index)])
            if math.isnan(score):
                continue
            memory = self._store.get(cache.memory_ids[int(index)])
            if memory is None:
                continue
            if memory_type is not None and memory.get("memory_type") != memory_type:
                continue
            if int(memory.get("sensitivity", 0)) < int(min_sensitivity):
                continue
            results.append((memory, score))
        return results

    def _cached_matrix(
        self,
        *,
        memory_type: str | None,
        min_sensitivity: int,
    ) -> _VectorMatrixCache:
        key = _cache_key(
            memory_type,
            min_sensitivity,
            self.provider.name,
            self.provider.model,
            self.dimension,
        )
        signature = self._store.vector_index_state(
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
            memory_type=memory_type,
            min_sensitivity=min_sensitivity,
        )
        cached = self._matrix_cache.get(key)
        if cached is not None and cached.signature == signature:
            return cached
        rows = self._store.vector_rows(
            provider=self.provider.name,
            model=self.provider.model,
            dimension=self.dimension,
            memory_type=memory_type,
            min_sensitivity=min_sensitivity,
        )
        memory_ids, matrix = _build_normalized_matrix(rows)
        cached = _VectorMatrixCache(signature=signature, memory_ids=memory_ids, matrix=matrix)
        self._matrix_cache[key] = cached
        return cached


def build_embedding_indexer(
    store: MemoryStore,
    *,
    provider_name: str = "hashing",
    model: str = "hashing-v1",
    dimension: int = 384,
    registry: EmbeddingProviderRegistry | None = None,
) -> MemoryEmbeddingIndexer:
    reg = registry or default_embedding_registry
    return MemoryEmbeddingIndexer(
        store,
        reg.build(provider_name, dimension=dimension, model=model),
    )
