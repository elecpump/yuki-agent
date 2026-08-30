from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from yuki.logger import get_logger
from yuki.memory.store import MemoryStore
from yuki.model_cache import ModelCacheManager

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

    def __init__(
        self,
        *,
        dimension: int = 384,
        model: str = "hashing-v1",
        cache_dir: str = "",
        device: str = "auto",
    ) -> None:
        # cache_dir/device 仅用于与语义 provider 保持统一构造签名；hashing 不使用。
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


class SentenceTransformerEmbeddingProvider:
    """Semantic embeddings via sentence-transformers（可选依赖，懒加载）。

    - 维度**从模型自身读取**（首次加载后确定）；配置的 `dimension` 仅对 hashing 有意义，
      此处忽略，保证 DB 键 (provider, model, dimension) 始终与模型实际维度一致。
    - `cache_dir` 指向 HF hub 缓存目录（如 `.model`，本地快照直接命中；缺省的
      `modules.json`/`1_Pooling` 等 sentence-transformers 元文件首次加载会从 hub 补齐）。
    - 加载/推理失败抛出异常：调用方（MemoryManager）捕获后降级 lexical/FTS。
    - 输出已 L2 归一化（normalize_embeddings=True，等价官方 2_Normalize 模块）。
    """

    name = "sentence-transformers"

    def __init__(
        self,
        *,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        dimension: int | None = None,
        cache_dir: str = "",
        device: str = "auto",
        sentence_transformer_factory: Callable[..., object] | None = None,
    ) -> None:
        self.model = model
        self._cache_dir = cache_dir
        self._device = device
        self._factory = sentence_transformer_factory
        self._st = None
        self._dimension: int | None = None
        self._lock = threading.Lock()

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def _ensure(self):
        st = self._st
        if st is not None:
            return st
        with self._lock:
            if self._st is not None:
                return self._st
            factory = self._factory
            if factory is None:
                # 懒导入：未安装 sentence-transformers 时模块级可导入、hashing 路径不受影响。
                from sentence_transformers import SentenceTransformer

                factory = SentenceTransformer
            self._st = factory(
                self.model,
                cache_folder=self._cache_dir or None,
                device=self._device,
            )
            self._dimension = int(self._st.get_sentence_embedding_dimension())
            return self._st

    def embed(self, texts: list[str]) -> list[list[float]]:
        st = self._ensure()
        vectors = st.encode(texts, normalize_embeddings=True)
        return [[float(value) for value in row] for row in vectors]


default_embedding_registry = EmbeddingProviderRegistry()
default_embedding_registry.register(
    "hashing",
    lambda **kwargs: HashingEmbeddingProvider(**kwargs),
)
default_embedding_registry.register(
    "sentence-transformers",
    lambda **kwargs: SentenceTransformerEmbeddingProvider(**kwargs),
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
    def __init__(
        self,
        store: MemoryStore,
        provider: EmbeddingProvider,
        *,
        cache_manager: ModelCacheManager | None = None,
    ) -> None:
        self._store = store
        self.provider = provider
        self._cache_manager = cache_manager
        self._matrix_cache: dict[
            tuple[str, str, int, str | None, int],
            _VectorMatrixCache,
        ] = {}

    @property
    def dimension(self) -> int:
        value = self.provider.dimension
        if value is None:
            # 语义 provider 懒加载未完成：任何维度查询都匹配不到记录，等价"尚未嵌入"。
            # upsert/search 均先 embed（触发加载）再使用本属性，故 0 不会写入 DB。
            return 0
        return int(value)

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
        self.clear_cache()
        return True

    def clear_cache(self) -> int:
        if self._cache_manager is not None:
            return self._cache_manager.clear("memory_vector_matrix")
        count = len(self._matrix_cache)
        self._matrix_cache.clear()
        return count

    def delete(self, memory_id: int) -> int:
        deleted = self._store.delete_embeddings(memory_id)
        self.clear_cache()
        return deleted

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
        include_ids: Iterable[int] = (),
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
        order = [int(index) for index in top_k_indices(scores, limit)]
        selected = set(order)
        index_by_memory_id = {
            memory_id: index for index, memory_id in enumerate(cache.memory_ids)
        }
        for memory_id in include_ids:
            index = index_by_memory_id.get(int(memory_id))
            if index is not None and index not in selected:
                order.append(index)
                selected.add(index)
        results: list[tuple[dict, float]] = []
        for index in order:
            score = float(scores[index])
            if math.isnan(score):
                continue
            memory = self._store.get(cache.memory_ids[index])
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
        cached = self._get_cached_matrix(key)
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
        self._put_cached_matrix(key, cached)
        return cached

    def _get_cached_matrix(
        self,
        key: tuple[str, str, int, str | None, int],
    ) -> _VectorMatrixCache | None:
        if self._cache_manager is None:
            return self._matrix_cache.get(key)
        value = self._cache_manager.get("memory_vector_matrix", key)
        return value if isinstance(value, _VectorMatrixCache) else None

    def _put_cached_matrix(
        self,
        key: tuple[str, str, int, str | None, int],
        cached: _VectorMatrixCache,
    ) -> None:
        if self._cache_manager is None:
            self._matrix_cache[key] = cached
            return
        weight = int(cached.matrix.nbytes) + len(cached.memory_ids) * 8
        self._cache_manager.put("memory_vector_matrix", key, cached, weight=weight)


class EmbeddingOutboxProcessor(Protocol):
    def process_embedding_outbox(self, *, limit: int = 20) -> int: ...


class EmbeddingOutboxWorker:
    """Memory-layer maintenance worker for deferred embedding mutations."""

    def __init__(self, processor: EmbeddingOutboxProcessor) -> None:
        self._processor = processor

    def tick(self, *, limit: int = 20) -> int:
        return int(self._processor.process_embedding_outbox(limit=limit))


def build_embedding_indexer(
    store: MemoryStore,
    *,
    provider_name: str = "hashing",
    model: str = "hashing-v1",
    dimension: int = 384,
    cache_dir: str = "",
    device: str = "auto",
    registry: EmbeddingProviderRegistry | None = None,
    cache_manager: ModelCacheManager | None = None,
) -> MemoryEmbeddingIndexer:
    reg = registry or default_embedding_registry
    return MemoryEmbeddingIndexer(
        store,
        reg.build(
            provider_name,
            dimension=dimension,
            model=model,
            cache_dir=cache_dir,
            device=device,
        ),
        cache_manager=cache_manager,
    )
