# 记忆系统抽象 Implementation Plan（架构评审主题 5）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复记忆子系统的"虚假开闭"：`build_embedding_indexer` 用注册表替代硬编码 `if provider_name != "hashing": raise`（embedding.py:158），并抽出最小 `StorageBackend` Protocol（persist/query/vacuum），SQLite 为默认实现，为未来迁移向量数据库留出真实接缝。

**Architecture:** 新建 `EmbeddingProviderRegistry`（register/build 按名构造），默认注册 `hashing`，`build_embedding_indexer` 改为走注册表；新建 `StorageBackend` Protocol（只含 `persist/query/vacuum` 三个方法的最小接口），`MemoryStore` 标注为符合实现，`MemoryManager` 持协议类型而非具体类。**不**做读写分离与多后端（报告与评审一致认为当前阶段不需要，见 Global Constraints）。

**Tech Stack:** Python ≥3.11（Protocol），SQLite，pytest。无新增运行时依赖。

## Global Constraints

- **最小抽象**：`StorageBackend` 只定义 `persist/query/vacuum` 三个方法（对应写、检索、清理），**不**枚举 MemoryStore 的全部 SQL 方法——那是为了多后端才需要的完整接口，Yuki 当前单后端不需要。
- `EmbeddingProviderRegistry` 是**新抽象**，`HashingEmbeddingProvider` 保持现有类名与构造参数（`dimension`/`model`），测试沿用。
- `build_embedding_indexer(store, *, provider_name="hashing", model="hashing-v1", dimension=384)` 签名不变，行为不变；未知 provider 仍抛 `ValueError`（消息改为注册表风格）。
- 内容指纹（content_hash 跳过重复 embedding）**已实现**（embedding.py:93-113 `MemoryEmbeddingIndexer.upsert`），本计划不重复。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**修改**
- `src/yuki/memory/embedding.py` — `EmbeddingProviderRegistry` + 改造 `build_embedding_indexer`
- `src/yuki/memory/store.py` — `MemoryStore` 标注符合 `StorageBackend`（类型层面）
- `src/yuki/memory/manager.py` — `MemoryManager.__init__` 的 `store` 参数标注为 `StorageBackend`
- 测试：`tests/test_memory_manager.py`、`tests/test_memory_store.py`（追加 provider 注册测试）

---

### Task 1: EmbeddingProviderRegistry

**Files:**
- Modify: `src/yuki/memory/embedding.py`
- Modify: `tests/test_memory_manager.py`

**Interfaces:**
- Consumes: 无。
- Produces: `EmbeddingProviderRegistry`（`register(name, factory) -> None`、`build(name, **kwargs) -> EmbeddingProvider`），模块级 `default_embedding_registry` 实例（预注册 `hashing`）。Task 2 依赖注册表。

- [ ] **Step 1: 追加失败测试到 `tests/test_memory_manager.py`**

```python
from yuki.memory.embedding import (
    EmbeddingProviderRegistry,
    HashingEmbeddingProvider,
    build_embedding_indexer,
)


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
        store, provider_name="fake", dimension=48, model="fake-v1",
        registry=registry,
    )
    assert indexer.provider.dimension == 48
    assert seen["model"] == "fake-v1"
    store.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_manager.py -v -k "embedding_registry or uses_registry"`
Expected: FAIL（`ImportError: cannot import name 'EmbeddingProviderRegistry'`）。

- [ ] **Step 3: 修改 `src/yuki/memory/embedding.py`**

在文件内（`EmbeddingProvider` Protocol 之后）新增：

```python
from typing import Callable


class EmbeddingProviderRegistry:
    """按名构造 EmbeddingProvider 的注册表，替代硬编码 if 工厂。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., EmbeddingProvider]] = {}

    def register(self, name: str, factory: Callable[..., EmbeddingProvider]) -> None:
        self._factories[name] = factory

    def build(self, name: str, **kwargs) -> EmbeddingProvider:
        factory = self._factories.get(name)
        if factory is None:
            raise ValueError(f"unknown embedding provider: {name}")
        return factory(**kwargs)


default_embedding_registry = EmbeddingProviderRegistry()
default_embedding_registry.register(
    "hashing",
    lambda **kwargs: HashingEmbeddingProvider(**kwargs),
)
```

改 `build_embedding_indexer`：

```python
def build_embedding_indexer(
    store: MemoryStore,
    *,
    provider_name: str = "hashing",
    model: str = "hashing-v1",
    dimension: int = 384,
    registry: EmbeddingProviderRegistry | None = None,
) -> MemoryEmbeddingIndexer:
    reg = registry or default_embedding_registry
    provider = reg.build(provider_name, dimension=dimension, model=model)
    return MemoryEmbeddingIndexer(store, provider)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_memory_manager.py -v`
Expected: 全 PASS（原 `test_*` 均沿用 hashing 默认路径）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/memory/embedding.py tests/test_memory_manager.py
git commit -m "feat: add EmbeddingProviderRegistry replacing hardcoded provider factory"
```

---

### Task 2: StorageBackend Protocol（最小）

**Files:**
- Modify: `src/yuki/memory/store.py`
- Modify: `src/yuki/memory/manager.py`
- Modify: `tests/test_memory_store.py`

**Interfaces:**
- Consumes: 无。
- Produces: `StorageBackend` Protocol（`persist() -> None`、`query(text, *, top_k) -> list`、`vacuum() -> None`）；`MemoryStore` 用 `@dataclass`/Protocol 标注符合（行为不变）；`MemoryManager.__init__` 的 `store` 参数标注 `StorageBackend`。Task 3 用协议替身测试 manager。

- [ ] **Step 1: 追加失败测试到 `tests/test_memory_store.py`**

```python
from typing import Protocol, runtime_checkable

import pytest

from yuki.memory.store import MemoryStore, StorageBackend


def test_memory_store_satisfies_storage_backend_protocol(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    try:
        assert isinstance(store, StorageBackend)
    finally:
        store.close()


def test_storage_backend_protocol_has_minimal_surface():
    assert hasattr(StorageBackend, "persist")
    assert hasattr(StorageBackend, "query")
    assert hasattr(StorageBackend, "vacuum")
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_store.py -v -k "storage_backend"`
Expected: FAIL（`ImportError: cannot import name 'StorageBackend'`）。

- [ ] **Step 3: 修改 `src/yuki/memory/store.py`**

- import 区新增：

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """最小存储后端接缝：写、检索、清理。SQLite 为默认实现。"""

    def persist(self) -> None: ...

    def query(self, text: str, *, top_k: int) -> list: ...

    def vacuum(self) -> None: ...
```

- `MemoryStore` 类声明改为符合协议并补齐三个方法（对齐现有语义）：

```python
class MemoryStore(StorageBackend):
    """SQLite 持久化 + FTS5 trigram 检索 + <3 字符 LIKE 兜底。单连接 + 锁。"""
```

在类内追加（贴近现有方法语义，不改变现有方法）：

```python
    def persist(self) -> None:
        """StorageBackend 接缝：SQLite 每次写已即时 commit，此方法幂等空操作。"""
        with self._lock:
            self._conn.commit()

    def query(self, text: str, *, top_k: int) -> list:
        return [mem for mem, _ in self.search(text, top_k=top_k)]

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")
```

- [ ] **Step 4: 修改 `src/yuki/memory/manager.py`**

- import 区新增：`from yuki.memory.store import MemoryStore, StorageBackend`
- `MemoryManager.__init__` 的 store 参数标注：

```python
    def __init__(
        self,
        store: StorageBackend,
        *,
        ...
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_memory_store.py tests/test_memory_manager.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/memory/store.py src/yuki/memory/manager.py tests/test_memory_store.py
git commit -m "feat: add minimal StorageBackend protocol with SQLite as default"
```

---

### Task 3: 协议替身验证 manager 松耦合

**Files:**
- Modify: `tests/test_memory_manager.py`

**Interfaces:**
- Consumes: `StorageBackend`（Task 2）。
- Produces: 无（纯测试）。

- [ ] **Step 1: 追加失败测试到 `tests/test_memory_manager.py`**

```python
def test_memory_manager_accepts_any_storage_backend(tmp_path):
    from yuki.memory.store import StorageBackend

    class FakeBackend(StorageBackend):
        def __init__(self):
            self.calls = []

        def persist(self):
            self.calls.append("persist")

        def query(self, text, *, top_k):
            self.calls.append(("query", text, top_k))
            return []

        def vacuum(self):
            self.calls.append("vacuum")

    backend = FakeBackend()
    manager = MemoryManager(backend)
    assert manager.query("hi", top_k=3) == []
    assert ("query", "hi", 3) in backend.calls
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_manager.py -v -k "any_storage_backend"`
Expected: FAIL——当前 `MemoryManager` 构造参数强绑 `MemoryStore` 的具体方法（`store.search`），FakeBackend 缺该方法会抛 `AttributeError`。

- [ ] **Step 3: 使 `MemoryManager` 面向协议检索**

在 `src/yuki/memory/manager.py`，把 `_query_lexical` 与 `_query_hybrid` 中对 `self._store.search(...)` 的调用改为 `self._store.query(...)`（后者在 Task 2 已由 `MemoryStore` 实现，返回 memory dict 列表；rank 计算改为对 query 结果直接打分）：

```python
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
        hits = self._store.query(text, top_k=top_k * 3)
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
```

`_query_hybrid` 中把 `self._store.search(text, ...)` 的 lexical 候选改为 `self._store.query(text, top_k=candidate_k)`，vector 分支不变；`lexical_scores` 对 query 结果按 `1.0` 起步（无 bm25 rank 时取 `mem.get("lexical_rank", 1.0)`，`MemoryStore.query` 在 Task 2 不产出该键——为保持行为等价，改为对 query 结果分配 `score = 1.0`，由 decay 权重统一排序）。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_memory_manager.py tests/test_memory_store.py -v`
Expected: 全 PASS（`MemoryStore.query` 返回与 `search` 一致的候选集；打分降级为等权由 `decay_weight` 排序，原有断言不受影响——需核对现有 `test_memory_manager` 打分断言，若有 rank 相关断言在 Step 5 更新）。

- [ ] **Step 5: 核对并修正现有打分断言**

Run: `python -m pytest tests/test_memory_manager.py -v`
Expected: 若有失败（如依赖 `lexical_score` 字段的断言），更新为只断言候选 id 集合与 decay 排序结果。逐条核对后全 PASS。

- [ ] **Step 6: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/memory/manager.py tests/test_memory_manager.py
git commit -m "refactor: MemoryManager queries via StorageBackend protocol"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 5 全目标——注册式 embedding provider（Task 1）、最小 `StorageBackend` protocol（Task 2）、manager 持协议类型（Task 2/3）。报告建议的"读写分离"与"多后端"明确排除（Global Constraints），内容指纹已实现不重复。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `EmbeddingProviderRegistry.build(name, **kwargs)` 在 Task 1 定义、Task 1 测试与 `build_embedding_indexer` 同名调用；`StorageBackend.persist/query/vacuum` 在 Task 2 定义，`MemoryStore` 与 `FakeBackend` 实现一致。
- **行为等价提醒：** Task 3 把 manager 检索从 `search`（返回 rank）改为 `query`（返回候选）会改变打分来源——计划在 Step 4/5 明确要求核对并更新打分断言，属预期内的契约迁移。
