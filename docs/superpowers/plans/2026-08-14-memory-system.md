# Yuki MemoryManager 记忆系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现纯 SQLite + FTS5 的记忆系统（五类记忆：短期/偏好/个人信息/场景/反思），提供 MemoryManager 门面 + CLI + `memory/*` 总线服务，接入 CognitionAgent。

**Architecture:** 新增 `src/yuki/memory/` 包，`MemoryStore`（SQLite 持久化 + FTS5 trigram + LIKE 兜底 + 触发器同步）→ `MemoryManager`（衰减加权检索、清理、短期工作记忆）→ `service.py`（总线 REQ/REP 接线）+ `cli.py`（离线直连 DB 的管理工具）。CognitionAgent.setup() 注册总线服务并新增 memory 健康组件。零运行时行为变化。

**Tech Stack:** Python ≥3.11，stdlib `sqlite3`（内置 FTS5，本机 SQLite 3.53.1），pydantic v2，pytest。无新增运行时依赖。

## Global Constraints

- 零新增运行时依赖：只用 stdlib `sqlite3`（FTS5 + trigram tokenizer 已内置）。
- 零协议变更：总线只新增 `memory/*` 服务名，不触碰现有主题/服务/wire format。
- 零运行时行为变化：只在 cognition setup 时多注册一组 REQ/REP handler；e2e 断言不变。
- `memory_type` 枚举：`preference` / `personal` / `scenario` / `reflection`（短期 `short_term` 仅驻内存不落盘）。
- `sensitivity`：0=普通 / 1=私密 / 2=高敏；`sensitivity==2` 的记忆为未来云端检索排除对象。
- `last_access` 初值 = `created_at`（新建记忆首次被访问后才切换为真实访问时间）。
- `personal` 类型与 `strengthened==1` 的记忆**排除**在自动清理外。
- trigram 对 <3 字符查询不可用 → `search` 对最短分词 <3 字符的查询走 `LIKE '%q%'` 兜底。
- `wipe` = `DELETE FROM memories`（DELETE 触发器同步 FTS）+ `INSERT INTO memories_fts(memories_fts) VALUES('rebuild')`。
- 每个任务结束跑 `python -m pytest <本次测试> -v`；全仓回归 `python -m pytest`（e2e 默认跳过）。
- 设计文档：`docs/superpowers/specs/2026-08-14-memory-system-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/memory/__init__.py`
- `src/yuki/memory/store.py` — `MemoryStore` + `MemoryError`
- `src/yuki/memory/manager.py` — `MemoryManager` + `ShortTermMemory` + `Reflector`
- `src/yuki/memory/service.py` — `register_memory_services`
- `src/yuki/memory/cli.py` + `src/yuki/memory/__main__.py` — CLI
- `tests/test_memory_store.py`、`tests/test_memory_manager.py`、`tests/test_memory_cli.py`、`tests/test_memory_service.py`

**修改**
- `src/yuki/config.py`（`MemoryConfig` + 注册进 load 循环）、`config.example.yaml`（memory 节）
- `src/yuki/payloads.py`（memory 相关 TypedDict）
- `src/yuki/cognition/agent.py`（注入 `memory`、setup 注册服务、teardown close、memory 健康组件）
- `tests/test_config.py`、`tests/cognition/test_cognition.py`
- `.gitignore`（加 `data/`）

---

### Task 1: Config memory 节 + payloads TypedDict

**Files:**
- Modify: `src/yuki/config.py`、`config.example.yaml`、`src/yuki/payloads.py`、`.gitignore`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Config.memory`（`MemoryConfig`：`db_path="data/yuki.db"`、`decay_base=1.0`、`decay_lambda=0.1`、`decay_threshold=0.02`、`short_term_ttl_s=1800`、`short_term_capacity=50`）；env `YUKI_MEMORY_<FIELD>`。`payloads.py` 新增 `MemoryWritePayload`/`MemoryQueryPayload`/`MemoryListPayload`/`MemoryGetPayload`/`MemoryDeletePayload`/`MemoryStrengthenPayload`/`MemoryResult`/`MemoryWriteResult`。Task 2/3/5 依赖这些名字。

- [ ] **Step 1: 追加失败测试到 `tests/test_config.py`**

```python
def test_memory_defaults():
    config = Config()
    assert config.memory.db_path == "data/yuki.db"
    assert config.memory.decay_base == 1.0
    assert config.memory.decay_lambda == 0.1
    assert config.memory.decay_threshold == 0.02
    assert config.memory.short_term_ttl_s == 1800
    assert config.memory.short_term_capacity == 50


def test_memory_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_MEMORY_DB_PATH", "tmp/mem.db")
    monkeypatch.setenv("YUKI_MEMORY_DECAY_LAMBDA", "0.3")
    config = Config.load(None)
    assert config.memory.db_path == "tmp/mem.db"
    assert config.memory.decay_lambda == 0.3
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_config.py::test_memory_defaults tests/test_config.py::test_memory_env_override -v`
Expected: FAIL（`AttributeError: 'Config' object has no attribute 'memory'`）。

- [ ] **Step 3: `src/yuki/config.py` 加 MemoryConfig 并注册**

在 `class HealthConfig` 之后新增：

```python
class MemoryConfig(BaseModel):
    db_path: str = "data/yuki.db"
    decay_base: float = Field(1.0, ge=0.0)
    decay_lambda: float = Field(0.1, ge=0.0)
    decay_threshold: float = Field(0.02, ge=0.0)
    short_term_ttl_s: float = Field(1800, ge=1)
    short_term_capacity: int = Field(50, ge=1)
```

在 `class Config` 中 `health` 字段之后新增：

```python
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
```

在 `Config.load` 的 `for section_name, section_cls in (...)` 元组中，`("health", HealthConfig),` 之后新增：

```python
            ("memory", MemoryConfig),
```

- [ ] **Step 4: `config.example.yaml` 加 memory 节**

```yaml
memory:
  db_path: data/yuki.db
  decay_base: 1.0
  decay_lambda: 0.1
  decay_threshold: 0.02
  short_term_ttl_s: 1800
  short_term_capacity: 50
```

- [ ] **Step 5: `.gitignore` 忽略 data 目录**

在 `.gitignore` 末尾追加一行：

```
data/
```

- [ ] **Step 6: `src/yuki/payloads.py` 追加 memory TypedDict**

在文件末尾追加：

```python
class MemoryWritePayload(TypedDict):
    memory_type: str
    content: str
    confidence: NotRequired[float]
    sensitivity: NotRequired[int]
    source: NotRequired[str]
    metadata: NotRequired[dict]


class MemoryQueryPayload(TypedDict):
    text: str
    type: NotRequired[str]
    top_k: NotRequired[int]
    min_sensitivity: NotRequired[int]


class MemoryListPayload(TypedDict):
    type: NotRequired[str]
    min_sensitivity: NotRequired[int]


class MemoryGetPayload(TypedDict):
    id: int


class MemoryDeletePayload(TypedDict):
    id: int


class MemoryStrengthenPayload(TypedDict):
    id: int


class MemoryResult(TypedDict):
    id: int
    memory_type: str
    content: str
    confidence: float
    sensitivity: int
    source: str
    metadata: dict
    created_at: float
    last_access: float
    access_count: int
    strengthened: bool
    score: NotRequired[float]


class MemoryWriteResult(TypedDict):
    id: int
```

- [ ] **Step 7: 运行验证通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/config.py config.example.yaml .gitignore src/yuki/payloads.py tests/test_config.py
git commit -m "feat: add memory config section and memory payload types"
```

---

### Task 2: MemoryStore（SQLite + FTS5 + LIKE 兜底）

**Files:**
- Create: `src/yuki/memory/__init__.py`、`src/yuki/memory/store.py`
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）。
- Produces: `MemoryError(Exception)`；`MemoryStore(db_path)` 方法 `create(memory_type, content, *, confidence=0.5, sensitivity=0, source="cli", metadata=None) -> int`、`get(id) -> dict | None`、`delete(id) -> bool`、`list(*, memory_type=None, min_sensitivity=0) -> list[dict]`、`all() -> list[dict]`、`touch(id, at=None) -> None`、`strengthen(id) -> bool`、`search(text, *, memory_type=None, top_k=5, min_sensitivity=0) -> list[tuple[dict, float]]`、`wipe() -> int`、`ping() -> bool`、`close()`。返回的 dict 键：`id/memory_type/content/confidence/sensitivity/source/metadata/created_at/last_access/access_count/strengthened`（`metadata` 已解析为 dict、`strengthened` 为 bool）。Task 3/4/5 依赖这些签名。

- [ ] **Step 1: 写失败测试 `tests/test_memory_store.py`**

```python
import pytest

from yuki.memory.store import MemoryError, MemoryStore


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def test_create_and_get(store):
    mem_id = store.create("preference", "用户喜欢安静的环境")
    mem = store.get(mem_id)
    assert mem["id"] == mem_id
    assert mem["memory_type"] == "preference"
    assert mem["content"] == "用户喜欢安静的环境"
    assert mem["confidence"] == 0.5
    assert mem["sensitivity"] == 0
    assert mem["source"] == "cli"
    assert mem["metadata"] == {}
    assert mem["strengthened"] is False
    assert mem["last_access"] == mem["created_at"]


def test_create_rejects_unknown_type(store):
    with pytest.raises(MemoryError):
        store.create("unknown", "x")


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_delete_returns_rowcount(store):
    mem_id = store.create("preference", "a")
    assert store.delete(mem_id) is True
    assert store.delete(mem_id) is False


def test_list_filters_type_and_sensitivity(store):
    store.create("preference", "喜欢茶", sensitivity=0)
    store.create("preference", "喜欢咖啡", sensitivity=1)
    store.create("scenario", "在读书", sensitivity=0)
    prefs = store.list(memory_type="preference")
    assert len(prefs) == 2
    only_high = store.list(min_sensitivity=1)
    assert [m["content"] for m in only_high] == ["喜欢咖啡"]


def test_search_cjk_two_char_via_like_fallback(store):
    store.create("preference", "用户喜欢量子计算")
    store.create("preference", "用户在研究股票")
    hits = store.search("计算")
    assert len(hits) == 1
    assert hits[0][0]["content"] == "用户喜欢量子计算"


def test_search_english_substring_via_fts(store):
    store.create("scenario", "user likes quantum computing")
    store.create("scenario", "user likes cooking")
    hits = store.search("quant")
    assert len(hits) == 1
    assert hits[0][0]["content"] == "user likes quantum computing"


def test_search_filters_and_limits(store):
    for i in range(6):
        store.create("preference", f"喜欢话题{i}")
    hits = store.search("话题", top_k=3, memory_type="preference")
    assert len(hits) == 3


def test_search_empty_text_returns_empty(store):
    store.create("preference", "a")
    assert store.search("") == []
    assert store.search("   ") == []


def test_touch_updates_last_access_and_count(store):
    mem_id = store.create("preference", "a")
    before = store.get(mem_id)
    store.touch(mem_id, at=before["created_at"] + 86400.0)
    after = store.get(mem_id)
    assert after["last_access"] > before["last_access"]
    assert after["access_count"] == 1


def test_strengthen_marks_and_resets_last_access(store):
    mem_id = store.create("preference", "a")
    old = store.get(mem_id)
    store.touch(mem_id, at=old["created_at"] - 86400.0)
    assert store.strengthen(mem_id) is True
    mem = store.get(mem_id)
    assert mem["strengthened"] is True
    assert mem["last_access"] > old["created_at"]
    assert store.strengthen(999) is False


def test_wipe_clears_all(store):
    store.create("preference", "a")
    store.create("scenario", "b")
    assert store.wipe() == 2
    assert store.all() == []


def test_ping_true_for_valid_db(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    assert s.ping() is True
    s.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.memory'`）。

- [ ] **Step 3: 创建 `src/yuki/memory/__init__.py`（Task 2 阶段保持空文件）**

```python
# 空文件。Task 3 完成后恢复从 manager 导出的便捷别名。
```

- [ ] **Step 4: 创建 `src/yuki/memory/store.py`**

```python
import json
import sqlite3
import threading
import time
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.memory.store")

MEMORY_TYPES = ("preference", "personal", "scenario", "reflection")


class MemoryError(Exception):
    """记忆存储错误。"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id            INTEGER PRIMARY KEY,
            memory_type   TEXT NOT NULL CHECK (memory_type IN ('preference','personal','scenario','reflection')),
            content       TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 0.5,
            sensitivity   INTEGER NOT NULL DEFAULT 0 CHECK (sensitivity IN (0,1,2)),
            source        TEXT NOT NULL DEFAULT 'cli',
            metadata      TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            last_access   REAL NOT NULL,
            access_count  INTEGER NOT NULL DEFAULT 0,
            strengthened  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
        "content, content='memories', content_rowid='id', tokenize='trigram')"
    )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )


def _uses_fts(text: str) -> bool:
    """最短分词 >=3 字符才用 FTS；否则 trigram 无法匹配，走 LIKE 兜底。"""
    return min((len(tok) for tok in text.split()), default=0) >= 3


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


class MemoryStore:
    """SQLite 持久化 + FTS5 trigram 检索 + <3 字符 LIKE 兜底。单连接 + 锁。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        _ensure_schema(self._conn)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def _row(self, row) -> dict:
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        d["strengthened"] = bool(d["strengthened"])
        return d

    def create(
        self,
        memory_type: str,
        content: str,
        *,
        confidence: float = 0.5,
        sensitivity: int = 0,
        source: str = "cli",
        metadata: dict | None = None,
    ) -> int:
        if memory_type not in MEMORY_TYPES:
            raise MemoryError(f"unknown memory_type: {memory_type!r}")
        now = time.time()
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (memory_type, content, confidence, sensitivity, source, metadata, created_at, last_access) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (memory_type, content, float(confidence), int(sensitivity), source, meta, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row(row) if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> list[dict]:
        sql = "SELECT * FROM memories WHERE sensitivity >= ?"
        params: list = [int(min_sensitivity)]
        if memory_type is not None:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memories").fetchall()
        return [self._row(r) for r in rows]

    def touch(self, memory_id: int, at: float | None = None) -> None:
        now = time.time() if at is None else at
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_access = ?, access_count = access_count + 1 WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()

    def strengthen(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET strengthened = 1, last_access = ? WHERE id = ?",
                (time.time(), memory_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def search(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
    ) -> list[tuple[dict, float]]:
        """返回 [(memory, rank), ...]，rank 高者更相关；LIKE 兜底路径 rank=1.0。"""
        text = (text or "").strip()
        if not text:
            return []
        min_sens = int(min_sensitivity)
        if _uses_fts(text):
            sql = (
                "SELECT m.*, bm25(memories_fts) AS bm25 "
                "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.sensitivity >= ?"
            )
            params: list = [_fts_phrase(text), min_sens]
            if memory_type is not None:
                sql += " AND m.memory_type = ?"
                params.append(memory_type)
            sql += " ORDER BY bm25 LIMIT ?"
            params.append(int(top_k))
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [(self._row(r), 1.0 / (1.0 + abs(r["bm25"]))) for r in rows]
        sql = (
            "SELECT * FROM memories "
            "WHERE content LIKE '%' || ? || '%' AND sensitivity >= ?"
        )
        params = [text, min_sens]
        if memory_type is not None:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(top_k))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(self._row(r), 1.0) for r in rows]

    def wipe(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            self._conn.execute("DELETE FROM memories")
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._conn.commit()
        return n
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/memory/__init__.py src/yuki/memory/store.py tests/test_memory_store.py
git commit -m "feat: add MemoryStore with FTS5 and like fallback"
```

---

### Task 3: MemoryManager（衰减/清理/短期记忆/Reflector）

**Files:**
- Create: `src/yuki/memory/manager.py`
- Modify: `src/yuki/memory/__init__.py`（恢复 manager 导入）
- Test: `tests/test_memory_manager.py`

**Interfaces:**
- Consumes: `MemoryStore`（Task 2 签名）、`MemoryConfig`（Task 1）。
- Produces: `ShortTermMemory(ttl_s=1800, capacity=50)` 方法 `add(content, *, kind="event")`/`items(now=None) -> list[dict]`/`recent(n=5, now=None)`/`clear()`；`Reflector.generate(scenario_ids, context=None) -> list[str]`（抛 `NotImplementedError`）；`MemoryManager(store, *, decay_base=1.0, decay_lambda=0.1, decay_threshold=0.02, short_term=None)` 方法 `write(...) -> int`、`get(id)`、`delete(id) -> bool`、`list(*, memory_type=None, min_sensitivity=0)`、`query(text, *, memory_type=None, top_k=5, min_sensitivity=0) -> list[dict]`（含 `score`）、`decay_weight(memory, now=None)`、`strengthen(id) -> bool`、`cleanup() -> int`、`wipe() -> int`、`ping() -> bool`、`short_term_add(content, *, kind="event")`、`short_term_items()`、`close()`。Task 4/5 依赖这些签名。

- [ ] **Step 1: 写失败测试 `tests/test_memory_manager.py`**

```python
import pytest

from yuki.memory.manager import MemoryManager, Reflector, ShortTermMemory
from yuki.memory.store import MemoryStore


@pytest.fixture()
def manager(tmp_path):
    m = MemoryManager(
        MemoryStore(tmp_path / "mem.db"),
        decay_base=1.0, decay_lambda=1.0, decay_threshold=0.3,
    )
    yield m
    m.close()


def test_write_returns_id_and_query_ranks_freshness(manager):
    old_id = manager.write("preference", "旧记忆", source="cli")
    manager._store.touch(old_id, at=1000000.0)  # 10 天前
    fresh_id = manager.write("preference", "新鲜记忆", source="cli")
    results = manager.query("记忆", top_k=5)
    assert results[0]["id"] == fresh_id


def test_query_returns_scores_and_touches(manager):
    mem_id = manager.write("preference", "喜欢咖啡")
    manager._store.touch(mem_id, at=1000000.0)
    results = manager.query("咖啡")
    assert results[0]["score"] > 0.0
    assert manager.get(mem_id)["access_count"] >= 1


def test_decay_weight_strengthened_is_one(manager):
    mem_id = manager.write("preference", "x")
    manager.strengthen(mem_id)
    mem = manager.get(mem_id)
    assert manager.decay_weight(mem, now=2000000000.0) == 1.0


def test_decay_weight_decays_over_time(manager):
    mem_id = manager.write("preference", "x")
    mem = manager.get(mem_id)
    fresh = manager.decay_weight(mem, now=mem["created_at"] + 86400.0)
    old = manager.decay_weight(mem, now=mem["created_at"] + 86400.0 * 10)
    assert fresh > old


def test_cleanup_removes_stale_but_keeps_personal_and_strengthened(manager):
    stale = manager.write("scenario", "旧场景")
    manager._store.touch(stale, at=1000000.0)
    personal = manager.write("personal", "我的名字")
    manager._store.touch(personal, at=1000000.0)
    strong = manager.write("preference", "强化项")
    manager.strengthen(strong)
    manager._store.touch(strong, at=1000000.0)
    deleted = manager.cleanup()
    assert deleted == 1
    ids = [m["id"] for m in manager.list()]
    assert stale not in ids
    assert personal in ids
    assert strong in ids


def test_wipe_and_ping(manager):
    manager.write("preference", "a")
    assert manager.ping() is True
    assert manager.wipe() == 1


def test_short_term_ttl_evicts_expired():
    st = ShortTermMemory(ttl_s=10, capacity=3)
    st.add("a", at=100.0)
    st.add("b", at=200.0)
    assert [it["content"] for it in st.items(now=205.0)] == ["b"]
    assert [it["content"] for it in st.items(now=215.0)] == []


def test_short_term_capacity_evicts_oldest():
    st = ShortTermMemory(ttl_s=100, capacity=3)
    for i in range(4):
        st.add(f"item{i}", at=float(i))
    assert [it["content"] for it in st.items(now=50.0)] == ["item3", "item2", "item1"]


def test_reflector_generate_not_implemented():
    with pytest.raises(NotImplementedError):
        Reflector().generate([1, 2])
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_manager.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.memory.manager'`）。

- [ ] **Step 3: 创建 `src/yuki/memory/manager.py`**

```python
import math
import time
from collections import deque

from yuki.memory.store import MemoryStore


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
        self._items.append({"content": content, "kind": kind, "ts": time.time() if at is None else at})
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
    """记忆门面：衰减加权检索、清理策略、短期工作记忆。"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        decay_base: float = 1.0,
        decay_lambda: float = 0.1,
        decay_threshold: float = 0.02,
        short_term: ShortTermMemory | None = None,
    ) -> None:
        self._store = store
        self._base = decay_base
        self._lam = decay_lambda
        self._threshold = decay_threshold
        self._short_term = short_term or ShortTermMemory()

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
        return self._store.create(
            memory_type, content,
            confidence=confidence, sensitivity=sensitivity,
            source=source, metadata=metadata,
        )

    def get(self, memory_id: int) -> dict | None:
        mem = self._store.get(memory_id)
        if mem is not None:
            self._store.touch(memory_id)
        return mem

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
    ) -> list[dict]:
        now = time.time()
        hits = self._store.search(
            text, memory_type=memory_type, top_k=top_k * 3, min_sensitivity=min_sensitivity,
        )
        scored: list[dict] = []
        for mem, rank in hits:
            self._store.touch(mem["id"])
            mem["score"] = rank * self.decay_weight(mem, now)
            scored.append(mem)
        scored.sort(key=lambda m: m["score"], reverse=True)
        return scored[:top_k]

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
        deleted = 0
        for mem in self._store.all():
            if mem["memory_type"] == "personal" or mem["strengthened"]:
                continue
            if self.decay_weight(mem, now) < self._threshold:
                if self._store.delete(mem["id"]):
                    deleted += 1
        return deleted

    def wipe(self) -> int:
        return self._store.wipe()

    def ping(self) -> bool:
        return self._store.ping()

    def short_term_add(self, content: str, *, kind: str = "event") -> None:
        self._short_term.add(content, kind=kind)

    def short_term_items(self) -> list[dict]:
        return self._short_term.items()

    def close(self) -> None:
        self._store.close()
```

- [ ] **Step 4: 将 `src/yuki/memory/__init__.py` 替换为便捷别名**

```python
from yuki.memory.manager import MemoryManager  # noqa: F401
from yuki.memory.store import MemoryError, MemoryStore  # noqa: F401
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_memory_manager.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/memory/manager.py src/yuki/memory/__init__.py tests/test_memory_manager.py
git commit -m "feat: add MemoryManager with decay, cleanup and short-term memory"
```

---

### Task 4: CLI（离线直连 DB）

**Files:**
- Create: `src/yuki/memory/cli.py`、`src/yuki/memory/__main__.py`
- Test: `tests/test_memory_cli.py`

**Interfaces:**
- Consumes: `MemoryManager`/`MemoryStore`/`MemoryError`（Task 2/3）。
- Produces: `cli.main(argv=None) -> int`；子命令 `list`/`query`/`add`/`get`/`delete`/`strengthen`/`wipe`/`short-term`。`python -m yuki.memory` 入口。退出码：成功 0、错误 1、用法 2（argparse 默认）。

- [ ] **Step 1: 写失败测试 `tests/test_memory_cli.py`**

```python
import io

import pytest

from yuki.memory.cli import main
from yuki.memory.store import MemoryStore


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "mem.db")


def test_add_then_list(db, capsys):
    assert main(["--db", db, "add", "--type", "preference", "--content", "用户喜欢茶",
                 "--source", "user", "--metadata", "topic=茶"]) == 0
    assert main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "用户喜欢茶" in out
    assert "preference" in out


def test_query_returns_scored_rows(db, capsys):
    main(["--db", db, "add", "--type", "scenario", "--content", "在读量子计算"])
    main(["--db", db, "add", "--type", "scenario", "--content", "在听音乐"])
    assert main(["--db", db, "query", "计算"]) == 0
    out = capsys.readouterr().out
    assert "在读量子计算" in out
    assert "score=" in out


def test_get_missing_returns_error_code(db):
    assert main(["--db", db, "get", "999"]) == 1


def test_delete_and_strengthen(db):
    assert main(["--db", db, "add", "--type", "personal", "--content", "名字叫小羽"]) == 0
    assert main(["--db", db, "strengthen", "1"]) == 0
    assert main(["--db", db, "delete", "1"]) == 0


def test_wipe_requires_confirmation(db, monkeypatch, capsys):
    main(["--db", db, "add", "--type", "preference", "--content", "x"])
    monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))
    assert main(["--db", db, "wipe"]) == 1
    assert MemoryStore(db).all() != []
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    assert main(["--db", db, "wipe"]) == 0
    assert MemoryStore(db).all() == []


def test_wipe_force_skips_prompt(db, monkeypatch, capsys):
    main(["--db", db, "add", "--type", "preference", "--content", "y"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--db", db, "wipe", "--force"]) == 0
    assert MemoryStore(db).all() == []


def test_short_term_view_is_empty(db, capsys):
    assert main(["--db", db, "short-term"]) == 0
    assert capsys.readouterr().out == ""
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_cli.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.memory.cli'`）。

- [ ] **Step 3: 创建 `src/yuki/memory/cli.py`**

```python
import argparse
import json
import sys

from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryError, MemoryStore


def build_manager(db_path: str, decay_base=1.0, decay_lambda=0.1, decay_threshold=0.02) -> MemoryManager:
    return MemoryManager(
        MemoryStore(db_path),
        decay_base=decay_base, decay_lambda=decay_lambda, decay_threshold=decay_threshold,
    )


def _fmt(mem: dict) -> str:
    meta = json.dumps(mem.get("metadata") or {}, ensure_ascii=False)
    score = mem.get("score")
    score_s = f" score={score:.3f}" if score is not None else ""
    return (
        f"#{mem['id']} [{mem['memory_type']}] conf={mem['confidence']} "
        f"sens={mem['sensitivity']} src={mem['source']} strong={mem['strengthened']} "
        f"last={mem['last_access']:.1f}{score_s} :: {mem['content']} (meta={meta})"
    )


def _cmd_list(args, manager: MemoryManager) -> None:
    for mem in manager.list(memory_type=args.type, min_sensitivity=args.min_sensitivity):
        print(_fmt(mem))


def _cmd_query(args, manager: MemoryManager) -> None:
    for mem in manager.query(
        args.text, memory_type=args.type, top_k=args.top_k, min_sensitivity=args.min_sensitivity,
    ):
        print(_fmt(mem))


def _cmd_add(args, manager: MemoryManager) -> None:
    metadata = {}
    if args.metadata:
        for pair in args.metadata:
            key, _, value = pair.partition("=")
            metadata[key] = value
    mem_id = manager.write(
        args.type, args.content,
        confidence=args.confidence, sensitivity=args.sensitivity,
        source=args.source, metadata=metadata,
    )
    print(mem_id)


def _cmd_get(args, manager: MemoryManager) -> int:
    mem = manager.get(args.id)
    if mem is None:
        print(f"memory #{args.id} not found", file=sys.stderr)
        return 1
    print(_fmt(mem))
    return 0


def _cmd_delete(args, manager: MemoryManager) -> None:
    print(manager.delete(args.id))


def _cmd_strengthen(args, manager: MemoryManager) -> None:
    print(manager.strengthen(args.id))


def _cmd_wipe(args, manager: MemoryManager) -> int:
    if not args.force:
        print("This will permanently delete ALL memories. Type 'yes' to confirm:", end=" ")
        sys.stdout.flush()
        if sys.stdin.readline().strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    print(manager.wipe())
    return 0


def _cmd_short_term(args, manager: MemoryManager) -> None:
    for item in manager.short_term_items():
        print(f"[{item['kind']}] {item['content']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yuki.memory", description="Yuki memory store admin")
    parser.add_argument("--db", default="data/yuki.db", help="SQLite db path")
    parser.add_argument("--decay-base", type=float, default=1.0)
    parser.add_argument("--decay-lambda", type=float, default=0.1)
    parser.add_argument("--decay-threshold", type=float, default=0.02)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--type")
    p.add_argument("--min-sensitivity", type=int, default=0)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("query")
    p.add_argument("text")
    p.add_argument("--type")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-sensitivity", type=int, default=0)
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("add")
    p.add_argument("--type", required=True,
                   choices=("preference", "personal", "scenario", "reflection"))
    p.add_argument("--content", required=True)
    p.add_argument("--confidence", type=float, default=0.5)
    p.add_argument("--sensitivity", type=int, default=0)
    p.add_argument("--source", default="cli")
    p.add_argument("--metadata", action="append")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("get")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_get)

    p = sub.add_parser("delete")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_delete)

    p = sub.add_parser("strengthen")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_strengthen)

    p = sub.add_parser("wipe")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_wipe)

    p = sub.add_parser("short-term")
    p.set_defaults(func=_cmd_short_term)

    args = parser.parse_args(argv)
    try:
        manager = build_manager(args.db, args.decay_base, args.decay_lambda, args.decay_threshold)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = args.func(args, manager)
        return result or 0
    except MemoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.close()
```

- [ ] **Step 4: 创建 `src/yuki/memory/__main__.py`**

```python
import sys

from yuki.memory.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_memory_cli.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 冒烟验证真实 CLI**

Run: `python -m yuki.memory --db data/smoke.db add --type preference --content 测试记忆; python -m yuki.memory --db data/smoke.db query 测试; python -m yuki.memory --db data/smoke.db wipe --force`
Expected: 依次输出 id、包含"测试记忆"的行、0。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/memory/cli.py src/yuki/memory/__main__.py tests/test_memory_cli.py
git commit -m "feat: add memory admin CLI"
```

---

### Task 5: 总线服务 + CognitionAgent 接线

**Files:**
- Create: `src/yuki/memory/service.py`
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Test: `tests/test_memory_service.py`

**Interfaces:**
- Consumes: `MemoryManager`（Task 3）、`MemoryError`（Task 2）、`CognitionAgent`（现有 `ProcessAgent`）。
- Produces: `register_memory_services(bus, manager)`；服务名 `memory/write|query|list|get|delete|strengthen|wipe`。`CognitionAgent.__init__` 新增 `memory: MemoryManager | None = None` 参数；`setup()` 创建/注册；`teardown()` close；`health_components()` 新增 `memory` 检查。

- [ ] **Step 1: 写失败测试 `tests/test_memory_service.py`**

```python
import pytest

from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryError, MemoryStore

from tests.fakes import FakeBus


@pytest.fixture()
def bus_and_manager(tmp_path):
    bus = FakeBus()
    manager = MemoryManager(MemoryStore(tmp_path / "mem.db"))
    register_memory_services(bus, manager)
    yield bus, manager
    manager.close()


def test_write_then_query(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "preference", "content": "用户喜欢猫"})["id"]
    assert rid > 0
    results = bus.request("memory/query", {"text": "猫", "top_k": 3})["results"]
    assert results[0]["content"] == "用户喜欢猫"
    assert "score" in results[0]


def test_list_and_get(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "scenario", "content": "在读某文"})["id"]
    listed = bus.request("memory/list", {})["results"]
    assert len(listed) == 1
    got = bus.request("memory/get", {"id": rid})["memory"]
    assert got["content"] == "在读某文"


def test_get_missing_raises_memory_error(bus_and_manager):
    bus, _ = bus_and_manager
    with pytest.raises(MemoryError):
        bus.request("memory/get", {"id": 999})


def test_delete_strengthen_wipe(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "personal", "content": "小羽"})["id"]
    assert bus.request("memory/strengthen", {"id": rid})["ok"] is True
    assert bus.request("memory/delete", {"id": rid})["deleted"] is True
    rid2 = bus.request("memory/write", {"memory_type": "preference", "content": "x"})["id"]
    assert bus.request("memory/wipe", {})["deleted_count"] == 1
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_memory_service.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.memory.service'`）。

- [ ] **Step 3: 创建 `src/yuki/memory/service.py`**

```python
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryError

MEMORY_SERVICES = (
    "memory/write", "memory/query", "memory/list", "memory/get",
    "memory/delete", "memory/strengthen", "memory/wipe",
)


def _require(value, message):
    if value is None:
        raise MemoryError(message)
    return {"memory": value}


def register_memory_services(bus, manager: MemoryManager) -> None:
    """注册 memory/* REQ/REP 服务。handler 抛出的异常由 BusNode 转 response error。"""

    def on_write(payload: dict) -> dict:
        return {"id": manager.write(
            payload["memory_type"], payload["content"],
            confidence=payload.get("confidence", 0.5),
            sensitivity=payload.get("sensitivity", 0),
            source=payload.get("source", "cli"),
            metadata=payload.get("metadata"),
        )}

    def on_query(payload: dict) -> dict:
        return {"results": manager.query(
            payload["text"],
            memory_type=payload.get("type"),
            top_k=payload.get("top_k", 5),
            min_sensitivity=payload.get("min_sensitivity", 0),
        )}

    def on_list(payload: dict) -> dict:
        return {"results": manager.list(
            memory_type=payload.get("type"),
            min_sensitivity=payload.get("min_sensitivity", 0),
        )}

    bus.respond("memory/write", on_write)
    bus.respond("memory/query", on_query)
    bus.respond("memory/list", on_list)
    bus.respond("memory/get", lambda p: _require(manager.get(p["id"]), "memory not found"))
    bus.respond("memory/delete", lambda p: {"deleted": manager.delete(p["id"])})
    bus.respond("memory/strengthen", lambda p: {"ok": manager.strengthen(p["id"])})
    bus.respond("memory/wipe", lambda p: {"deleted_count": manager.wipe()})
```

- [ ] **Step 4: 更新 `tests/cognition/test_cognition.py`（注入 memory，断言服务注册）**

```python
from yuki.cognition.agent import CognitionAgent
from yuki.config import Config
from yuki.memory.manager import MemoryManager
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakePipeline:
    def warmup_vlm(self):
        pass


def test_cognition_agent_wires_pipeline_responder_and_memory(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    assert Topics.AWAKE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert all(service in bus.services for service in MEMORY_SERVICES)
    agent.teardown()


def test_cognition_agent_health_includes_memory(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    components = agent.health_components()
    assert "memory" in components
    status = components["memory"]()
    assert status.ok is True
```

- [ ] **Step 5: 修改 `src/yuki/cognition/agent.py`**

```python
from yuki.cognition.l1 import L1Engine
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.process import ProcessAgent


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None,
                 memory: MemoryManager | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._responder = None
        self._memory = memory

    def setup(self) -> None:
        if self._pipeline is None:
            self._pipeline = build_pipeline(
                self.bus,
                vlm=self._vlm,
                sensitive_filter=self._sensitive_filter,
                stt=self._stt,
                frame_client=self._frame_client,
                speech_buffer=self._speech_buffer,
            )
        self._pipeline.warmup_vlm()  # VLM 后台预热（不可用则降级文本模式）
        self._responder = build_l1_responder(self.bus, l1=self._l1 or L1Engine())
        if self._memory is None:
            self._memory = MemoryManager(
                MemoryStore(self.config.memory.db_path),
                decay_base=self.config.memory.decay_base,
                decay_lambda=self.config.memory.decay_lambda,
                decay_threshold=self.config.memory.decay_threshold,
            )
        register_memory_services(self.bus, self._memory)

    def teardown(self) -> None:
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "l1": self._health_l1,
            "pipeline": self._health_pipeline,
            "memory": self._health_memory,
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(False, {"reason": "no_vlm"})
        return HealthStatus(vlm._loaded, {"loaded": vlm._loaded})

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        return HealthStatus(stt is not None, {"installed": stt is not None})

    def _health_l1(self) -> HealthStatus:
        return HealthStatus(self._responder is not None, {"installed": self._responder is not None})

    def _health_pipeline(self) -> HealthStatus:
        frame_client = getattr(self._pipeline, "_frame_client", None) if self._pipeline else None
        ok = frame_client is not None and hasattr(frame_client, "get_latest")
        return HealthStatus(ok, {"frame_client_available": ok})

    def _health_memory(self) -> HealthStatus:
        ok = self._memory is not None and self._memory.ping()
        return HealthStatus(ok, {"db": self.config.memory.db_path})
```

- [ ] **Step 6: 运行验证通过**

Run: `python -m pytest tests/test_memory_service.py tests/cognition/test_cognition.py -v`
Expected: 全 PASS。

- [ ] **Step 7: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/memory/service.py src/yuki/cognition/agent.py tests/test_memory_service.py tests/cognition/test_cognition.py
git commit -m "feat: wire memory bus services into CognitionAgent"
```

---

## 自检记录

- **Spec 覆盖**：§2 四模块（store/manager/service/cli）+ Reflector + 短期记忆 → Task 2/3/4/5；§3 数据模型/FTS/LIKE 兜底 → Task 2；§4 衰减/检索/清理/强化 → Task 3；§5.1 CLI → Task 4；§5.2 总线服务 → Task 5；§5.3 配置 → Task 1；§5.4 健康 → Task 5；§6 错误处理/wipe → Task 2/4；§7 零新依赖/零协议变更 → 各任务约束。
- **一致性**：`MemoryStore.search` 返回 `(dict, rank)` 元组，`MemoryManager.query` 消费并注入 `score`；`register_memory_services` 的 `memory/*` 服务名与 `MEMORY_SERVICES` 常量、测试断言一致；`MemoryConfig` 字段名与 env 前缀 `YUKI_MEMORY_` 一致。
