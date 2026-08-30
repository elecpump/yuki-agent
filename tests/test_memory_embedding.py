from yuki.memory.embedding import (
    HashingEmbeddingProvider,
    MemoryEmbeddingIndexer,
    SentenceTransformerEmbeddingProvider,
    build_embedding_indexer,
    default_embedding_registry,
)
from yuki.memory.store import MemoryStore


class FakeSentenceTransformer:
    def __init__(self, dimension: int = 1024):
        self.dimension = dimension
        self.encode_calls = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, texts, normalize_embeddings=True):
        self.encode_calls.append((list(texts), normalize_embeddings))
        return [[1.0 / (index + 1)] * self.dimension for index in range(len(texts))]


def test_provider_derives_dimension_from_model_lazily():
    created = []

    def factory(model, cache_folder=None, device="auto"):
        created.append((model, cache_folder, device))
        return FakeSentenceTransformer(dimension=1024)

    provider = SentenceTransformerEmbeddingProvider(
        model="Qwen/Qwen3-Embedding-0.6B",
        cache_dir=".model",
        device="cuda:0",
        sentence_transformer_factory=factory,
    )
    assert provider.dimension is None  # 加载前维度未知，不触发导入

    vectors = provider.embed(["hello", "世界"])

    assert created == [("Qwen/Qwen3-Embedding-0.6B", ".model", "cuda:0")]
    assert provider.dimension == 1024
    assert len(vectors) == 2
    assert len(vectors[0]) == 1024


def test_provider_defaults_and_loads_once():
    created = []

    def factory(model, cache_folder=None, device="auto"):
        created.append((model, cache_folder, device))
        return FakeSentenceTransformer(dimension=1024)

    provider = SentenceTransformerEmbeddingProvider(sentence_transformer_factory=factory)
    provider.embed(["a"])
    provider.embed(["b"])

    # "auto" 必须解析为实际设备：sentence-transformers 6.x 不接受字面 "auto"。
    assert len(created) == 1
    assert created[0][0] == "Qwen/Qwen3-Embedding-0.6B"
    assert created[0][1] is None
    assert created[0][2] in ("cuda", "cpu")
    assert provider.embed(["c"])  # 不重复构造
    assert len(created) == 1


def test_provider_normalizes_embeddings():
    st = FakeSentenceTransformer(dimension=4)
    provider = SentenceTransformerEmbeddingProvider(
        sentence_transformer_factory=lambda model, **kwargs: st,
    )
    provider.embed(["x"])
    assert st.encode_calls == [(["x"], True)]


def test_registry_builds_sentence_transformers_provider():
    provider = default_embedding_registry.build(
        "sentence-transformers",
        model="Qwen/Qwen3-Embedding-0.6B",
        cache_dir=".model",
        device="auto",
        sentence_transformer_factory=lambda model, **kwargs: FakeSentenceTransformer(dimension=1024),
    )
    assert provider.name == "sentence-transformers"
    assert len(provider.embed(["ok"])[0]) == 1024


def test_hashing_provider_tolerates_semantic_kwargs():
    provider = HashingEmbeddingProvider(dimension=32, cache_dir=".model", device="cuda:0")
    assert provider.dimension == 32
    assert len(provider.embed(["x"])[0]) == 32


def test_build_embedding_indexer_passes_cache_and_device():
    indexer = build_embedding_indexer(
        MemoryStore(":memory:"),
        provider_name="hashing",
        model="hashing-v1",
        dimension=16,
        cache_dir=".model",
        device="cuda:0",
    )
    assert indexer.provider.name == "hashing"
    assert indexer.dimension == 16


def test_indexer_upsert_and_search_with_semantic_provider():
    store = MemoryStore(":memory:")
    provider = SentenceTransformerEmbeddingProvider(
        sentence_transformer_factory=lambda model, **kwargs: FakeSentenceTransformer(dimension=8),
    )
    indexer = MemoryEmbeddingIndexer(store, provider)

    memory_id = store.create("preference", "今天天气很好", confidence=0.9, source="test")
    memory = store.get(memory_id)

    assert indexer.upsert(memory) is True
    assert indexer.upsert(memory) is False  # 内容未变，幂等
    hits = indexer.search("天气", top_k=3)
    assert len(hits) == 1
    assert hits[0][0]["id"] == memory_id
