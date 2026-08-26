# Yuki 记忆混合检索（FTS5 + Vector）设计

> 日期：2026-08-18
> 状态：设计定稿，待实现
> 范围：在现有 `memories` + `memories_fts`（FTS5 trigram）之上新增向量召回，做 hybrid retrieval；保留 FTS 精确命中，补语义召回。默认 `vector_enabled=false`，现有检索行为完全不变。

## 1. 背景与目标

当前记忆检索是 SQLite FTS5 trigram + `<3` 字符 LIKE 兜底，`MemoryManager.query()` 用 `bm25` 归一化 rank × 衰减权重排序（`docs/2026-08-14-memory-system` 明确"向量检索留待后续"）。

本阶段把"留待后续"落地，但不替换 FTS5，而是 **hybrid retrieval**：

```
query
  -> FTS5/LIKE lexical candidates
  -> vector semantic candidates
  -> merge + decay + confidence + sensitivity filter
  -> top_k
```

这样保留中文关键词精确命中，同时补上"语义相似但字面不重合"的记忆召回。

**已确认决策**：
- **不替换 FTS5**，hybrid 双通道 merge。
- **独立 embedding 表**，不把向量塞进 `memories.metadata`。
- **第一版零向量库依赖**：SQLite 拉候选到 Python 算 cosine，几千到几万条记忆足够，不引入 faiss/hnswlib/sqlite-vss。
- **默认关闭**（`vector_enabled=false`），行为与当前 FTS5 完全等同；真语义 provider（sentence-transformers / 云端）后续接入。

## 2. 架构与文件布局

```
src/yuki/memory/
  embedding.py    — 新增：EmbeddingProvider(Protocol) + HashingEmbeddingProvider + MemoryEmbeddingIndexer
  store.py        — 改：PRAGMA foreign_keys=ON；memory_embeddings 表；upsert_embedding() / vector_candidates() / wipe() 显式清表
  manager.py      — 改：MemoryManager.__init__ 加可选 embedding_indexer / vector_enabled，默认旧行为；query() hybrid merge
  cli.py          — 改：新增 embeddings rebuild 子命令
tests/
  test_memory_store.py     — FK 级联、vector_candidates、多模型共存、wipe 无残留
  test_memory_manager.py   — vector off 行为等价、hybrid merge、candidate_k 扩张、embed 失败降级
  test_memory_embedding.py — 新增：provider 契约、hash provider、indexer upsert 幂等、rebuild 幂等
```

- `embedding.py`：provider 抽象 + 默认 hashing 实现 + 负责 embed 的 indexer。**MemoryStore 不直接依赖任何模型库。**
- `store.py`：只加"存向量 / 按模型取候选"的持久化能力，不碰模型。
- `manager.py`：只做 merge / 加权 / 衰减，不调模型；`MemoryManager` 不直接依赖模型库。

## 3. 存储模型

### 3.1 连接与 Schema 修正

`MemoryStore.__init__` 必须显式打开外键（SQLite 默认关闭，否则 `ON DELETE CASCADE` 不生效）：

```python
self._conn.execute("PRAGMA foreign_keys=ON")
self._conn.execute("PRAGMA journal_mode=WAL")
```

新增表（复合主键，多模型 embedding 可共存）：

```sql
CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  dimension     INTEGER NOT NULL,
  embedding     BLOB NOT NULL,
  content_hash  TEXT NOT NULL,
  updated_at    REAL NOT NULL,
  PRIMARY KEY (memory_id, provider, model, dimension)
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
ON memory_embeddings(provider, model, dimension);
```

复合主键带来的保证：
- 删除 `memories` 级联删 embedding。
- 多模型 embedding 可共存。
- 查询按当前 `(provider, model, dimension)` 过滤，不会误用旧模型。
- upsert 用 `INSERT ... ON CONFLICT(memory_id, provider, model, dimension) DO UPDATE`。

### 3.2 wipe() 显式清表

即使 FK 打开，也让"一键清除无残留"更直观：

```python
def wipe(self) -> int:
    # 先清 embedding，再清 memories（含 FTS 重建）
    DELETE FROM memory_embeddings
    DELETE FROM memories
    INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')
```

`delete()` / `delete_decayed()` 依赖 FK 级联，无需显式删 embedding。

### 3.3 向量格式

- 写入：`np.asarray(v, dtype="<f4").tobytes()`（显式 little-endian float32）。
- 读取：`np.frombuffer(blob, dtype="<f4")`。
- 不允许把 Python list 存进 BLOB。

## 4. Embedding 抽象

```python
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...

class HashingEmbeddingProvider:
    """零依赖默认 provider。字符 n-gram feature-hash + L2 归一化。

    定位：跑通 schema/index/query/rebuild 与 hybrid 管线，开发 baseline。
    不做语义宣称——本质是字面 overlap 的字符哈希。
    """

class MemoryEmbeddingIndexer:
    """负责 embed 的边界：MemoryStore 不依赖模型库，模型由本类持有。"""

    def __init__(self, store: MemoryStore, provider: EmbeddingProvider,
                 *, content_hash: Callable[[str], str] = sha256_hex) -> None:
        ...

    def upsert(self, memory_id: int, content: str) -> None:
        """按 content_hash 判重：hash 不变则跳过，避免重复 embed。"""

    def embed_query(self, text: str) -> list[float] | None:
        """查询侧 embed；provider 不可用/抛异常返回 None（上层降级 FTS）。"""

    def rebuild(self, *, memory_type: str | None = None,
                progress: Callable[[int, int], None] | None = None) -> int:
        """全库（或按 type）逐条 upsert，返回处理条数。分批处理，不一次全 load。"""
```

实现说明：
- **HashingEmbeddingProvider** 用字符 1/2-gram feature-hash 到 384 维并 L2 归一化，零依赖、稳定、与 FTS trigram 部分重叠但能覆盖单/双字查询。定位只是框架 baseline，不宣称语义。
- **真实语义（2026-08-26 已落地）**：`SentenceTransformerEmbeddingProvider`（`sentence-transformers`，已加入 `ml` extra，懒加载）——从模型自身读取维度（忽略配置的 `embedding_dimension`，保证 DB 键与实际维度一致），`cache_dir` 指向 HF hub 缓存（如 `.model`），`normalize_embeddings=True` 等价官方 `2_Normalize` 模块；默认模型 `Qwen/Qwen3-Embedding-0.6B`（last-token pooling，1024 维）。加载/推理失败由 `MemoryManager` 捕获降级 lexical/FTS。cloud embedding provider（默认关闭，走隐私策略）仍留后续。
- **content_hash**：对 `content` 做 sha256（记忆内容创建后不可变，无 update API，故 content 即可）。

## 5. 写入路径

`MemoryManager.write()` 后由 indexer 生成 embedding：

```
write memory
  -> store.create()
  -> if embedding_indexer: embedding_indexer.upsert(memory_id, content)
```

- 第一版同步（hash provider 快，代价可忽略）；后续 ST/云 provider 慢再改 background worker。
- 幂等：upsert 按 `content_hash` 判断是否重算；`ON CONFLICT DO UPDATE` 只在 hash 变化时更新。

## 6. 查询路径（hybrid merge）

### 6.1 候选数量

`vector_candidates` 不能固定吃死，查询时扩张：

```python
candidate_k = max(config.vector_candidates, top_k * 3)
```

`MemoryAccess.query()` 上层传 `top_k * 5`（privacy.py 现行为），这里收到的 `top_k` 已变大，仍能扩张。

### 6.2 vector_enabled 语义

`vector_enabled=false`（或 indexer 为 None）时**必须完全走旧路径**：

```python
if not vector_enabled or self._embedding_indexer is None:
    return old_fts_decay_query(...)
```

不能混入 confidence，不能改权重。只有 vector 开启后才使用：

```
score =
  lexical_weight * lexical_score
+ vector_weight   * vector_score
+ confidence_weight * confidence
then * decay_weight
```

- FTS 命中的 memory 得 `lexical_score`；vector 命中的得 `vector_score`；两边候选按 id merge（同一 id 双侧得分相加）。
- cosine 归一到 `[0, 1]`：`score = min(max((cosine + 1.0) / 2.0, 0.0), 1.0)`。
- 批量 cosine 用 numpy，不逐行解包。
- **只 touch 最终返回的 top_k**（当前已如此，保持）。
- `min_sensitivity` / `memory_type` 在两侧候选 SQL 里都过滤；personal/高敏过滤继续由 `min_sensitivity` 和 `MemoryAccess` 管。
- 查询侧 embed 失败（provider 不可用/异常）必须降级 FTS，不影响主路径。

### 6.3 空文本

`search()` 现对空文本返回 `[]`；hybrid 路径同样短路，不 embed 空串。

## 7. 配置

新增 `MemoryConfig` 字段（`Config(extra="forbid")`，加到现有 MemoryConfig 类即可，`load()` 的 env 映射天然覆盖 `MEMORY_*`）：

```yaml
memory:
  vector_enabled: false
  embedding_provider: hashing
  embedding_model: hashing-v1
  embedding_dimension: 384
  vector_candidates: 30
  lexical_weight: 0.45
  vector_weight: 0.45
  confidence_weight: 0.10
```

权重仅在 `vector_enabled=true` 时生效（见 6.2）。

## 8. 装配点

`MemoryManager.__init__` 加可选参数，默认保持兼容：

```python
def __init__(self, store, *, decay_base=1.0, decay_lambda=0.1, decay_threshold=0.02,
             short_term_ttl_s=1800, short_term_capacity=50, short_term=None,
             embedding_indexer: MemoryEmbeddingIndexer | None = None,
             vector_enabled: bool = False, ...)
```

必须覆盖三处构造点：
- `CognitionAssembler.assemble()`（assembly.py:90）：按 config 构造 indexer。
- `memory.cli build_manager`（cli.py:9）：按 config 构造 indexer，或默认无 indexer。
- 测试里直接 `MemoryManager(MemoryStore(...))`：不传 indexer → 旧行为。

## 9. CLI rebuild

```bash
python -m yuki.memory.cli embeddings rebuild [--type memory_type]
```

- 全库（或按 type）逐条 `upsert`，分批处理。
- 同一 content 重复 rebuild 不重算（content_hash 判重）。
- 背景：避免 cognition 启动时突然给全库算 embedding；按需手动 backfill。

## 10. 测试重点

- 写入 memory 后 embedding 表有记录。
- 同内容重复 rebuild 不重算（hash 判重，`upsert` 次数可数）。
- FTS 不命中但 hash overlap 命中时能召回（用单/双字查询构造；hash provider 是字符 overlap，不是语义，测试写"hash overlap 召回"而非"语义召回"）。
- hybrid merge 按 id 去重，最终只 touch top_k。
- `delete` / `wipe` / `delete_decayed` 后 embedding 同步删除（FK 级联 + wipe 显式清表）。
- provider/model/dimension 变更时旧 embedding 不被误用（复合主键共存 + 查询按当前模型过滤）。
- `vector_enabled=false` 时行为完全等同当前 FTS5（结果集 + score 均一致，不带 confidence）。
- `candidate_k = max(vector_candidates, top_k*3)` 扩张生效。
- 查询侧 embed 失败 → 降级 FTS，不抛异常。
- `vector_candidates` SQL 同时过滤 `sensitivity >= min_sensitivity` 与 `memory_type`。

## 11. 风险与兼容

- 兼容性：新增表 + 新配置项 + 可选构造参数，`vector_enabled=false` 时运行路径不变。
- 迁移：`_ensure_schema` 用 `CREATE TABLE IF NOT EXISTS`，旧库无迁移。
- 新增依赖：无（`ml` extra 不动，hashing provider 零依赖）。
- 性能：几万条 embedding 全表拉到 Python 算 cosine 为几十 ms 级；若后续量级上来自动化评估是否需要 faiss/hnswlib（本阶段不引入）。
- **范围外**：SentenceTransformer / 云 embedding provider、后台异步 embed worker、索引自维护、embedding 相似度阈值过滤。

## 12. 落地顺序

1. `store.py`：`PRAGMA foreign_keys=ON` + 复合主键 `memory_embeddings` 表 + `upsert_embedding()` + `vector_candidates()` + `wipe()` 显式清表。
2. `embedding.py`：`EmbeddingProvider` / `HashingEmbeddingProvider` / `MemoryEmbeddingIndexer`。
3. `manager.py`：可选 vector indexer，默认旧行为；`query()` hybrid merge + 加权评分。
4. `cli.py`：`embeddings rebuild` 子命令。
5. 测试：FK 级联、多模型共存、vector off 等价、candidate_k 扩张、embed 失败降级 FTS。
6. 再考虑真实 embedding provider（sentence-transformers / 云，默认关闭）。

## 13. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| hybrid 双通道，不替换 FTS5 | 保留中文关键词精确命中，补语义召回 |
| 独立 embedding 表，不进 metadata | 不污染 JSON 元数据；按 provider/model 可共存可清理 |
| 复合主键 `(memory_id, provider, model, dimension)` | 多模型共存；查询按当前模型过滤，旧模型不误用 |
| `PRAGMA foreign_keys=ON` | SQLite 默认关闭 FK，否则 ON DELETE CASCADE 是死代码 |
| wipe() 显式清 embedding 表 | 一键清除无残留，不依赖 FK 语义 |
| 第一版零向量库依赖 | 几万条内 SQLite + numpy cosine 足够；推迟 faiss 决策 |
| 默认 `vector_enabled=false`，走完全旧路径 | 行为等价可验证；风险最小 |
| hashing provider 只作框架 baseline | 零依赖跑通管线；不做语义宣称，语义增益留给 ST/云 |
| `candidate_k = max(vector_candidates, top_k*3)` | 避免固定候选池饿死大 top_k（MemoryAccess 传 top_k*5） |
| 查询侧 embed 失败降级 FTS | 向量是增量能力，失败不影响主路径 |
| CLI rebuild 而非启动时懒加载 | 避免 cognition 启动突然给全库算 embedding |
