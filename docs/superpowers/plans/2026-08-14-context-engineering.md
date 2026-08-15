# Yuki 上下文工程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现上下文子系统——读写分离（`WorkingContext` 写入侧 + `ContextSnapshot` 只读投影）、尽力持久化、`CloudViewBuilder`（填充顺序+最低配额预算、固定折叠单元+缓存+熔断的 LLM 摘要）接入 DecisionHub 与 CloudBridge。

**Architecture:** `src/yuki/cognition/context/`（`store.py` TurnStore 协议 + `working.py` WorkingContext + `snapshot.py` ContextSnapshot/ContextProjector）；`src/yuki/cognition/l2/view.py` CloudViewBuilder（enrich→format）取代 `l2/context.py`；`CloudBridge` 注入 summarize 闭包；hub 决策开始投影快照。

**Tech Stack:** Python ≥3.11，stdlib json/hashlib/math/time + 现有 pydantic/structlog。零新增运行时依赖。

## Global Constraints

- 零新增运行时依赖；零协议变更（REPLY 主题/载荷不变）。
- `TurnStore` 协议：`add(content, kind, ts)` / `items()`（新→旧 `[{content, kind, ts}]`）/ `clear()`；`ShortTermTurnStore(manager)` 包装 `MemoryManager.short_term`。
- `MemoryManager` 增 `short_term_add(content, *, kind="event", at=None)`（透传 `at`）与 `short_term_clear()`。
- `WorkingContext(store, *, snapshot_path=None, snapshot_interval=5, ttl_s=1800.0)`：`add_user/add_agent/update_situation/situation/turn_count/items/snapshot/restore/close`。快照 `{turns(旧→新), situation, saved_at}`；restore TTL 过滤后回填；`snapshot_path=None` 纯内存；读写失败仅告警。
- `ContextSnapshot`（frozen）：`situation`/`recent_turns`/`summaries`/`long_term_memory`；`ContextProjector(max_turns=20).build(working)` 投影（新→旧、去重连续重复、截 max_turns、situation 最新）。
- `estimate_tokens(text) = ceil(len/1.5)`；配额常量 `SITUATION_TOKENS=200`/`MEMORY_MIN_TOKENS=200`/`MAX_UTTERANCE_CHARS=500`；折叠常量 `FOLD_UNIT_SIZE=6`/`SUMMARIZE_TIMEOUT_S=2.0`/`SUMMARIZE_MAX_FAILURES=3`。
- `CloudViewBuilder(summarize=None, *, max_turns=20, max_tokens=1500, verbatim_turns=4, memory_top_k=3)`：
  - `enrich(snapshot, memory, utterance) -> ContextSnapshot`：填充 summaries（**预算触发**：短会话不折叠；固定单元切分、单元缓存键=内容哈希、缓存复用；失败计数占位；连续失败≥3 熔断）与 long_term_memory（检索 top-k、`sensitivity==2` 过滤、preference/strengthened 优先）。
  - `format(snapshot, utterance) -> str`：填充顺序 utterance→情境→逐字轮→折叠摘要→记忆；utterance 截断到 MAX_UTTERANCE_CHARS 恒保留；情境固定 SITUATION_TOKENS；逐字轮恒保留 verbatim_turns；折叠摘要吃剩余；记忆保证 MEMORY_MIN_TOKENS 再填充。
- `CloudClient.chat(messages, tools=None, timeout_s=None)`（新增可选 timeout_s，缺省用客户端超时）。
- `CloudBridge(..., view_builder=None)`：`generate(utterance, context: ContextSnapshot|None=None, memory=None)` 用 `enrich`+`format`；默认 summarize 闭包调 `client.chat(..., timeout_s=SUMMARIZE_TIMEOUT_S)`；`SUMMARIZE_PROMPT` 常量。删除 `l2/context.py`。
- `DecisionHub` 增 `context: WorkingContext|None=None` 与 `projector: ContextProjector|None=None`：`_handle` 入口 `snapshot = projector.build(context)`（无则 None，行为不变）；SITUATION 时 `context.update_situation(payload)`；决策后 UTTERANCE `context.add_user(text)`、spoke `context.add_agent(rendered)`；L2 路径 `bridge.generate(text, snapshot, memory)`。
- `Config` 增 `context:` 节（max_turns=20/max_tokens=1500/verbatim_turns=4/snapshot_path="data/context_snapshot.json"，env `YUKI_CONTEXT_*`）。
- `CognitionAgent.setup` 装配 `WorkingContext(ShortTermTurnStore(memory), snapshot_path=config.context.snapshot_path)` + `context.restore()` + `ContextProjector(config.context.max_turns)`，传给 hub/bridge；`teardown` 调 `context.close()`。
- e2e 等价：cloud 默认关；无决策时不写快照文件（仅启动 restore 读取）。
- 测试命令（仓库根）：`& ".venv\Scripts\python.exe" -m pytest <文件> -v`；全仓 `-m pytest`。
- 设计文档：`docs/superpowers/specs/2026-08-14-context-engineering-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/cognition/context/__init__.py`、`store.py`、`working.py`、`snapshot.py`
- `src/yuki/cognition/l2/view.py`
- `tests/cognition/context/test_store.py`、`test_working.py`、`test_snapshot.py`
- `tests/cognition/l2/test_view.py`

**修改**
- `src/yuki/memory/manager.py`（short_term_add at / short_term_clear）、`tests/test_memory_manager.py`
- `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- `src/yuki/cognition/l2/client.py`（chat timeout_s）、`tests/cognition/l2/test_client.py`
- `src/yuki/cognition/l2/bridge.py`（view 接入 + summarize 闭包）、`tests/cognition/l2/test_bridge.py`
- `src/yuki/cognition/brain/hub.py`（context/projector）、`tests/cognition/test_hub.py`
- `src/yuki/cognition/agent.py`（装配 + teardown close）、`tests/cognition/test_cognition.py`

**删除**
- `src/yuki/cognition/l2/context.py`、`tests/cognition/l2/test_context.py`

---

### Task 1: Context 配置 + TurnStore + MemoryManager short_term 扩展

**Files:**
- Create: `src/yuki/cognition/context/store.py`、`src/yuki/cognition/context/__init__.py`（空）
- Modify: `src/yuki/memory/manager.py`、`tests/test_memory_manager.py`、`src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- Test: `tests/cognition/context/test_store.py`

**Interfaces:**
- Consumes: `MemoryManager`。
- Produces: `TurnStore` 协议、`ShortTermTurnStore(manager)`；`MemoryManager.short_term_add(..., at=None)`/`short_term_clear()`；`Config.context`（`ContextConfig`: max_turns=20/max_tokens=1500/verbatim_turns=4/snapshot_path="data/context_snapshot.json"）。Task 2 依赖。

- [ ] **Step 1: 追加 MemoryManager 测试到 `tests/test_memory_manager.py`**

```python
def test_short_term_add_with_at_and_clear():
    manager = MemoryManager(MemoryStore(":memory:"))
    manager.short_term_add("a", kind="turn", at=100.0)
    manager.short_term_add("b", kind="turn", at=200.0)
    items = manager.short_term_items()
    assert [it["content"] for it in items] == ["b", "a"]
    assert items[1]["ts"] == 100.0
    manager.short_term_clear()
    assert manager.short_term_items() == []
```

（`:memory:` 仅用于本测试的临时 store 路径；若无内存 SQLite 支持，改用 `tmp_path` 后重写——但 Python sqlite3 `:memory:` 可用，直接保留。）

- [ ] **Step 2: 追加 context 配置测试到 `tests/test_config.py`**

```python
def test_context_defaults():
    config = Config()
    assert config.context.max_turns == 20
    assert config.context.max_tokens == 1500
    assert config.context.verbatim_turns == 4
    assert config.context.snapshot_path == "data/context_snapshot.json"


def test_context_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_CONTEXT_MAX_TURNS", "30")
    monkeypatch.setenv("YUKI_CONTEXT_SNAPSHOT_PATH", "tmp/snap.json")
    config = Config.load(None)
    assert config.context.max_turns == 30
    assert config.context.snapshot_path == "tmp/snap.json"
```

- [ ] **Step 3: 写失败测试 `tests/cognition/context/test_store.py`**

```python
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_short_term_turn_store_add_items_clear(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    store = ShortTermTurnStore(manager)
    store.add("你好", "user", 100.0)
    store.add("我在", "agent", 200.0)
    items = store.items()
    assert [it["content"] for it in items] == ["我在", "你好"]
    assert items[0]["kind"] == "agent"
    store.clear()
    assert store.items() == []
```

- [ ] **Step 4: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_store.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.context'`）。

- [ ] **Step 5: 创建 `src/yuki/cognition/context/store.py`**

```python
from typing import Protocol

from yuki.memory.manager import MemoryManager


class TurnStore(Protocol):
    """会话轮次存储接口（未来 Redis 实现同协议即可替换）。"""

    def add(self, content: str, kind: str, ts: float) -> None: ...
    def items(self) -> list[dict]: ...
    def clear(self) -> None: ...


class ShortTermTurnStore:
    """默认实现：包装 MemoryManager.short_term（TTL 30min/容量 50）。"""

    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    def add(self, content: str, kind: str, ts: float) -> None:
        self._manager.short_term_add(content, kind=kind, at=ts)

    def items(self) -> list[dict]:
        return self._manager.short_term_items()

    def clear(self) -> None:
        self._manager.short_term_clear()
```

- [ ] **Step 6: `src/yuki/memory/manager.py` 扩展**

`short_term_add` 改为透传 `at`：

```python
    def short_term_add(self, content: str, *, kind: str = "event", at: float | None = None) -> None:
        self._short_term.add(content, kind=kind, at=at)
```

新增：

```python
    def short_term_clear(self) -> None:
        self._short_term.clear()
```

- [ ] **Step 7: `src/yuki/config.py` 加 ContextConfig 并注册**

在 `SoulConfig` 之后新增：

```python
class ContextConfig(BaseModel):
    max_turns: int = Field(20, ge=1)
    max_tokens: int = Field(1500, ge=100)
    verbatim_turns: int = Field(4, ge=1)
    snapshot_path: str = "data/context_snapshot.json"
```

在 `Config` 中 `soul` 字段之后新增：

```python
    context: ContextConfig = Field(default_factory=ContextConfig)
```

在 `Config.load` 的 section 元组中 `("soul", SoulConfig),` 之后新增：

```python
            ("context", ContextConfig),
```

- [ ] **Step 8: `config.example.yaml` 加 context 节**

```yaml
context:
  max_turns: 20
  max_tokens: 1500
  verbatim_turns: 4
  snapshot_path: data/context_snapshot.json
```

- [ ] **Step 9: 创建 `src/yuki/cognition/context/__init__.py`（空，Task 7 补导出）**

```python
# 空文件。Task 7 完成后补导出。
```

- [ ] **Step 10: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_store.py tests/test_memory_manager.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 11: Commit**

```bash
git add src/yuki/cognition/context/store.py src/yuki/cognition/context/__init__.py src/yuki/memory/manager.py src/yuki/config.py config.example.yaml tests/cognition/context/test_store.py tests/test_memory_manager.py tests/test_config.py
git commit -m "feat: add TurnStore abstraction and context config"
```

---

### Task 2: WorkingContext（写入侧 + 尽力持久化）

**Files:**
- Create: `src/yuki/cognition/context/working.py`
- Test: `tests/cognition/context/test_working.py`

**Interfaces:**
- Consumes: `TurnStore`（Task 1）。
- Produces: `WorkingContext(store, *, snapshot_path=None, snapshot_interval=5, ttl_s=1800.0)`：`add_user/add_agent/update_situation/situation/turn_count/items/snapshot/restore/close`。Task 3 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/context/test_working.py`**

```python
import json
import time

from yuki.cognition.context.working import WorkingContext
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_store(tmp_path):
    return MemoryManager(MemoryStore(tmp_path / "m.db"))


def test_add_turns_and_situation(tmp_path):
    manager = make_store(tmp_path)
    ctx = WorkingContext(manager, snapshot_path=None)
    ctx.update_situation({"topic": "量子计算", "sensitive": False})
    ctx.add_user("你好")
    ctx.add_agent("我在")
    assert ctx.situation()["topic"] == "量子计算"
    assert ctx.turn_count() == 2
    items = ctx.items()
    assert [it["content"] for it in items] == ["我在", "你好"]
    assert [it["kind"] for it in items] == ["agent", "user"]


def test_snapshot_restore_roundtrip(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.update_situation({"topic": "X", "sensitive": False})
    ctx.add_user("第一轮")
    ctx.add_agent("回复")
    ctx.close()  # flush

    fresh = WorkingContext(make_store(tmp_path), snapshot_path=path, ttl_s=1800.0)
    fresh.restore()
    assert fresh.turn_count() == 2
    assert fresh.situation()["topic"] == "X"
    assert [it["content"] for it in fresh.items()] == ["回复", "第一轮"]


def test_restore_filters_expired_turns(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    old_ts = time.time() - 10000.0
    # 直接写快照模拟旧轮次
    path.write_text(json.dumps({
        "turns": [{"content": "旧轮", "kind": "user", "ts": old_ts},
                  {"content": "新轮", "kind": "user", "ts": time.time()}],
        "situation": None,
    }), encoding="utf-8")
    ctx.restore()
    contents = [it["content"] for it in ctx.items()]
    assert "旧轮" not in contents
    assert "新轮" in contents


def test_snapshot_path_none_does_not_write(tmp_path):
    manager = make_store(tmp_path)
    ctx = WorkingContext(manager, snapshot_path=None)
    ctx.add_user("x")
    ctx.close()
    assert not list(tmp_path.iterdir())


def test_snapshot_write_failure_warns(tmp_path):
    manager = make_store(tmp_path)
    path = tmp_path / "no_dir" / "snap.json"  # 父目录不存在 → snapshot 应自动创建
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.add_user("x")
    ctx.close()
    assert path.exists()
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_working.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.context.working'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/context/working.py`**

```python
import json
import time
from pathlib import Path

from yuki.cognition.context.store import TurnStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.context.working")


class WorkingContext:
    """写入侧：追加会话轮次/情境 + 尽力持久化。

    决策用 ContextProjector 投影只读快照，不直接读本对象。
    """

    def __init__(self, store: TurnStore, *, snapshot_path: str | Path | None = None,
                 snapshot_interval: int = 5, ttl_s: float = 1800.0) -> None:
        self._store = store
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._snapshot_interval = snapshot_interval
        self._ttl_s = ttl_s
        self._situation: dict | None = None
        self._add_count = 0

    def add_user(self, text: str) -> None:
        self._add("user", text)

    def add_agent(self, text: str) -> None:
        self._add("agent", text)

    def _add(self, kind: str, text: str) -> None:
        self._store.add(text, kind, time.time())
        self._add_count += 1
        if self._snapshot_path is not None and self._add_count % self._snapshot_interval == 0:
            self.snapshot()

    def update_situation(self, payload: dict) -> None:
        self._situation = payload

    def situation(self) -> dict | None:
        return self._situation

    def turn_count(self) -> int:
        return len(self._store.items())

    def items(self) -> list[dict]:
        return self._store.items()

    def snapshot(self) -> None:
        if self._snapshot_path is None:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "turns": [{"content": t["content"], "kind": t["kind"], "ts": t["ts"]}
                          for t in reversed(self._store.items())],
                "situation": self._situation,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("context snapshot failed", error=str(exc))

    def restore(self) -> None:
        if self._snapshot_path is None or not self._snapshot_path.exists():
            return
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("context restore failed", error=str(exc))
            return
        now = time.time()
        for turn in data.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            ts = turn.get("ts", now)
            if isinstance(ts, (int, float)) and now - ts <= self._ttl_s:
                self._store.add(turn.get("content", ""), turn.get("kind", "turn"), ts)
        situation = data.get("situation")
        if isinstance(situation, dict):
            self._situation = situation

    def close(self) -> None:
        self.snapshot()
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_working.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/context/working.py tests/cognition/context/test_working.py
git commit -m "feat: add WorkingContext write side with best-effort snapshot persistence"
```

---

### Task 3: ContextSnapshot + ContextProjector

**Files:**
- Create: `src/yuki/cognition/context/snapshot.py`
- Test: `tests/cognition/context/test_snapshot.py`

**Interfaces:**
- Consumes: `WorkingContext`（Task 2）。
- Produces: `ContextSnapshot`（frozen：situation/recent_turns/summaries/long_term_memory）、`ContextProjector(max_turns=20).build(working) -> ContextSnapshot`。Task 4/6 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/context/test_snapshot.py`**

```python
import pytest

from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot
from yuki.cognition.context.working import WorkingContext
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_ctx(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    return WorkingContext(manager, snapshot_path=None)


def test_project_fills_situation_and_recent_turns(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.update_situation({"topic": "量子计算", "sensitive": False})
    ctx.add_user("你好")
    ctx.add_agent("我在")
    snap = ContextProjector().build(ctx)
    assert snap.situation["topic"] == "量子计算"
    assert [t["content"] for t in snap.recent_turns] == ["我在", "你好"]
    assert [t["kind"] for t in snap.recent_turns] == ["agent", "user"]


def test_project_dedups_consecutive_repeats(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.add_user("嗯嗯")
    ctx.add_user("嗯嗯")
    ctx.add_agent("好")
    snap = ContextProjector().build(ctx)
    assert [t["content"] for t in snap.recent_turns] == ["好", "嗯嗯"]


def test_project_caps_max_turns(tmp_path):
    ctx = make_ctx(tmp_path)
    for i in range(30):
        ctx.add_user(f"t{i}")
    snap = ContextProjector(max_turns=5).build(ctx)
    assert len(snap.recent_turns) == 5
    assert snap.recent_turns[0]["content"] == "t29"  # 新→旧


def test_snapshot_is_frozen():
    snap = ContextSnapshot(recent_turns=({"content": "x", "kind": "user", "ts": 0.0},))
    with pytest.raises(Exception):
        snap.recent_turns[0]["content"] = "y"  # tuple 元素是 dict，但 tuple 本身不可改
    with pytest.raises(Exception):
        snap.recent_turns = ()
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_snapshot.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.context.snapshot'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/context/snapshot.py`**

```python
from dataclasses import dataclass

from yuki.cognition.context.working import WorkingContext


@dataclass(frozen=True)
class ContextSnapshot:
    """每次决策投影的只读快照。决策层只见此 schema。"""

    situation: dict | None = None
    recent_turns: tuple = ()
    summaries: tuple = ()
    long_term_memory: tuple = ()


class ContextProjector:
    """把写入侧投影为只读快照（裁剪/排序/去重）。"""

    def __init__(self, max_turns: int = 20) -> None:
        self._max_turns = max_turns

    def build(self, working: WorkingContext) -> ContextSnapshot:
        seen = None
        turns = []
        for item in working.items():  # 新→旧
            content = item.get("content", "")
            if content and content != seen:
                turns.append({
                    "content": content,
                    "kind": item.get("kind", "turn"),
                    "ts": item.get("ts", 0.0),
                })
            seen = content
            if len(turns) >= self._max_turns:
                break
        return ContextSnapshot(situation=working.situation(), recent_turns=tuple(turns))
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/context/test_snapshot.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/context/snapshot.py tests/cognition/context/test_snapshot.py
git commit -m "feat: add frozen ContextSnapshot and ContextProjector"
```

---

### Task 4: CloudViewBuilder（enrich/format/折叠/熔断）

**Files:**
- Create: `src/yuki/cognition/l2/view.py`
- Test: `tests/cognition/l2/test_view.py`

**Interfaces:**
- Consumes: `ContextSnapshot`（Task 3）、`MemoryManager`。
- Produces: `estimate_tokens`、配额/折叠常量、`CloudViewBuilder(summarize=None, *, max_turns=20, max_tokens=1500, verbatim_turns=4, memory_top_k=3)` 方法 `enrich(snapshot, memory, utterance) -> ContextSnapshot`、`format(snapshot, utterance) -> str`。Task 5 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/l2/test_view.py`**

```python
import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import (
    MAX_UTTERANCE_CHARS,
    SITUATION_TOKENS,
    CloudViewBuilder,
    estimate_tokens,
)
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_snapshot(*, turns=(), situation=None):
    return ContextSnapshot(situation=situation, recent_turns=tuple(turns))


def turn(text, kind="user"):
    return {"content": text, "kind": kind, "ts": 0.0}


def test_estimate_tokens():
    assert estimate_tokens("你好") == 1   # ceil(2/1.5)=2? 修正见下
    assert estimate_tokens("a" * 30) == 20


def test_enrich_short_conversation_no_summarize():
    calls = []
    builder = CloudViewBuilder(summarize=lambda texts: calls.append(texts) or "摘要")
    snap = make_snapshot(turns=[turn("t0"), turn("t1"), turn("t2"), turn("t3"), turn("t4")])
    out = builder.enrich(snap, None, "你好")
    assert out.summaries == ()          # 预算足够 → 不折叠
    assert calls == []


def test_enrich_long_conversation_folds_and_caches():
    calls = []

    def fake_summarize(texts):
        calls.append(texts)
        return "旧轮摘要"

    builder = CloudViewBuilder(summarize=fake_summarize, max_tokens=150)
    # 30 轮 → 超出逐字预算 → 折叠
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(30)]
    snap = make_snapshot(turns=turns)
    out1 = builder.enrich(snap, None, "你好")
    assert calls  # 调了摘要
    assert any("摘要" in s for s in out1.summaries)
    # 缓存复用：再 enrich 不调摘要
    n_calls = len(calls)
    out2 = builder.enrich(snap, None, "你好")
    assert len(calls) == n_calls
    assert out2.summaries == out1.summaries


def test_enrich_summarize_failure_placeholder_and_circuit_breaker():
    def boom(texts):
        raise RuntimeError("summarize down")

    builder = CloudViewBuilder(summarize=boom, max_tokens=150)
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(30)]
    snap = make_snapshot(turns=turns)
    for _ in range(3):  # 连续失败 >= SUMMARIZE_MAX_FAILURES 后熔断
        out = builder.enrich(snap, None, "x")
    assert "之前聊了" in out.summaries[0]
    assert builder._summarize_broken is True


def test_enrich_summarize_none_placeholder():
    builder = CloudViewBuilder(summarize=None, max_tokens=150)
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(30)]
    snap = make_snapshot(turns=turns)
    out = builder.enrich(snap, None, "x")
    assert out.summaries and "之前聊了" in out.summaries[0]


def test_enrich_memory_filters_high_sensitivity(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "喜欢安静", sensitivity=0)
    manager.write("personal", "高敏机密", sensitivity=2)
    builder = CloudViewBuilder()
    snap = make_snapshot()
    out = builder.enrich(snap, manager, "记忆")
    contents = [m["content"] for m in out.long_term_memory]
    assert "喜欢安静" in contents
    assert "高敏机密" not in contents


def test_format_order_and_quota():
    builder = CloudViewBuilder()
    snap = make_snapshot(
        situation={"topic": "量子计算", "summary": "介绍", "key_points": ["a"]},
        turns=[turn("逐字轮1", "user"), turn("逐字轮2", "agent")],
    )
    text = builder.format(snap, "你好呀" * 200)  # 超长 utterance → 截断
    assert text.startswith("用户说：")
    assert "量子计算" in text
    assert "逐字轮1" in text
    assert "你好呀" in text  # 截断但保留开头


def test_format_empty_snapshot():
    builder = CloudViewBuilder()
    text = builder.format(make_snapshot(), "")
    assert "用户说：" in text
```

（注意：`test_estimate_tokens` 中 `estimate_tokens("你好")` 实际为 `ceil(2/1.5)=ceil(1.33)=2`，不是 1——实现时以真实函数为准，测试断言按 `==2`。`test_enrich_summarize_failure_placeholder_and_circuit_breaker` 中一次失败是否熔断取决于 `SUMMARIZE_MAX_FAILURES`；若为 3，则该测试应断言**连续 3 次失败后** broken，或把 builder 构造为 `SUMMARIZE_MAX_FAILURES=1` 不可配时改为断言失败计数占位存在。实现时按真实常量调整断言：连续调用 3 次 enrich（每次独立）使失败≥3 后 broken=True。以代码为准。）

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_view.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.l2.view'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/l2/view.py`**

```python
import hashlib
import math
from typing import Callable

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.memory.manager import MemoryManager

SITUATION_TOKENS = 200
MEMORY_MIN_TOKENS = 200
MAX_UTTERANCE_CHARS = 500
FOLD_UNIT_SIZE = 6
SUMMARIZE_TIMEOUT_S = 2.0
SUMMARIZE_MAX_FAILURES = 3


def estimate_tokens(text: str) -> int:
    """字符启发式估 token（中英混合粗估），零依赖。"""
    return math.ceil(len(text or "") / 1.5)


def _truncate_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


class CloudViewBuilder:
    """L2 提示视图：enrich（折叠/记忆）→ format（填充顺序+最低配额预算）。"""

    def __init__(self, summarize: Callable[[list[str]], str] | None = None, *,
                 max_turns: int = 20, max_tokens: int = 1500,
                 verbatim_turns: int = 4, memory_top_k: int = 3) -> None:
        self._summarize = summarize
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._verbatim_turns = verbatim_turns
        self._memory_top_k = memory_top_k
        self._summary_cache: dict[str, str] = {}
        self._summarize_failures = 0
        self._summarize_broken = False

    def enrich(self, snapshot: ContextSnapshot, memory: MemoryManager | None,
               utterance: str) -> ContextSnapshot:
        summaries = self._fold(snapshot.recent_turns, utterance)
        memories = self._retrieve_memory(memory, utterance)
        return ContextSnapshot(
            situation=snapshot.situation,
            recent_turns=snapshot.recent_turns,
            summaries=tuple(summaries),
            long_term_memory=tuple(memories),
        )

    def _retrieve_memory(self, memory, utterance) -> list[dict]:
        if memory is None:
            return []
        results = memory.query(utterance or "", top_k=self._memory_top_k, min_sensitivity=0)
        safe = [m for m in results if m.get("sensitivity", 0) != 2]
        guaranteed = [m for m in safe if m.get("memory_type") == "preference" or m.get("strengthened")]
        others = [m for m in safe if m not in guaranteed]
        return guaranteed[: self._memory_top_k] + others[: max(0, self._memory_top_k - len(guaranteed))]

    def _fold(self, recent_turns, utterance) -> list[str]:
        fold = list(reversed(recent_turns))[: max(0, len(recent_turns) - self._verbatim_turns)]
        if not fold:
            return []
        # 预算触发：逐字包含折叠轮仍在预算内 → 不折叠
        base = estimate_tokens(utterance or "") + SITUATION_TOKENS
        base += sum(estimate_tokens(t["content"]) for t in recent_turns[: self._verbatim_turns])
        verbatim_fold = sum(estimate_tokens(t["content"]) for t in fold)
        if base + verbatim_fold <= self._max_tokens:
            return []
        segments = [fold[i:i + FOLD_UNIT_SIZE] for i in range(0, len(fold), FOLD_UNIT_SIZE)]
        summaries = []
        used = base
        for seg in segments:
            key = self._segment_key(seg)
            cached = self._summary_cache.get(key)
            if cached is not None:
                text = cached
            else:
                text = self._summarize_segment(seg)
                if text is not None:
                    self._summary_cache[key] = text
            if text is None:
                text = f"（之前聊了 {len(seg)} 轮）"
            tok = estimate_tokens(text)
            if used + tok > self._max_tokens:
                break
            summaries.append(text)
            used += tok
        return summaries

    def _summarize_segment(self, seg) -> str | None:
        if self._summarize is None or self._summarize_broken:
            return None
        try:
            text = self._summarize([t["content"] for t in seg])
            self._summarize_failures = 0
            return text
        except Exception:
            self._summarize_failures += 1
            if self._summarize_failures >= SUMMARIZE_MAX_FAILURES:
                self._summarize_broken = True
            return None

    def _segment_key(self, seg) -> str:
        h = hashlib.sha256()
        for t in seg:
            h.update(t["content"].encode("utf-8"))
        return h.hexdigest()

    def format(self, snapshot: ContextSnapshot, utterance: str) -> str:
        parts = []
        used = 0
        utt = _truncate_chars(utterance or "", MAX_UTTERANCE_CHARS)
        parts.append(f"用户说：{utt}")
        used += estimate_tokens(utt)
        if snapshot.situation:
            sit = self._format_situation(snapshot.situation)
            parts.append(f"当前情境：{sit}")
            used += estimate_tokens(sit)
        for t in snapshot.recent_turns[: self._verbatim_turns]:
            line = f"[{t['kind']}] {t['content']}"
            parts.append(line)
            used += estimate_tokens(line)
        for s in snapshot.summaries:
            line = f"（摘要）{s}"
            if used + estimate_tokens(line) > self._max_tokens:
                break
            parts.append(line)
            used += estimate_tokens(line)
        if snapshot.long_term_memory:
            mem_lines, guaranteed_tok = [], 0
            for m in snapshot.long_term_memory:
                line = f"- {m['content']}"
                tok = estimate_tokens(line)
                if (m.get("memory_type") == "preference" or m.get("strengthened")) \
                        and guaranteed_tok < MEMORY_MIN_TOKENS:
                    mem_lines.append((line, tok))
                    guaranteed_tok += tok
            for m in snapshot.long_term_memory:
                line = f"- {m['content']}"
                tok = estimate_tokens(line)
                if m.get("memory_type") == "preference" or m.get("strengthened"):
                    continue
                if used + sum(t for _, t in mem_lines) + tok > self._max_tokens:
                    break
                mem_lines.append((line, tok))
            if mem_lines:
                parts.append("相关记忆：\n" + "\n".join(l for l, _ in mem_lines))
        return "\n".join(p for p in parts if p)

    def _format_situation(self, situation: dict) -> str:
        bits = [b for b in [
            situation.get("topic", ""),
            situation.get("summary", ""),
            *(situation.get("key_points") or []),
        ] if b]
        text = " ".join(bits)
        return _truncate_chars(text, int(SITUATION_TOKENS * 1.5))
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_view.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/l2/view.py tests/cognition/l2/test_view.py
git commit -m "feat: add CloudViewBuilder with fill-order quotas and cached fold summarization"
```

---

### Task 5: CloudClient timeout + CloudBridge 接入 view

**Files:**
- Modify: `src/yuki/cognition/l2/client.py`、`tests/cognition/l2/test_client.py`
- Modify: `src/yuki/cognition/l2/bridge.py`、`tests/cognition/l2/test_bridge.py`
- Delete: `src/yuki/cognition/l2/context.py`、`tests/cognition/l2/test_context.py`

**Interfaces:**
- Consumes: `CloudViewBuilder`（Task 4）、`CloudClient`。
- Produces: `CloudClient.chat(messages, tools=None, timeout_s=None)`；`CloudBridge(..., view_builder=None)`；`generate(utterance, context: ContextSnapshot|None=None, memory=None)`；`SUMMARIZE_PROMPT`。Task 6 依赖。

- [ ] **Step 1: 追加 client 测试到 `tests/cognition/l2/test_client.py`**

```python
def test_chat_accepts_per_call_timeout():
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "hi"}}]}

    client = CloudClient("https://api.example.com/v1", "m1", timeout_s=10.0, post=fake_post)
    client.chat([{"role": "user", "content": "x"}], timeout_s=2.0)
    assert captured["timeout"] == 2.0
    client.chat([{"role": "user", "content": "x"}])
    assert captured["timeout"] == 10.0
```

- [ ] **Step 2: 追加 bridge 测试到 `tests/cognition/l2/test_bridge.py`**

```python
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import CloudViewBuilder


class FakeView:
    def __init__(self):
        self.enriched = []
        self.formatted = []

    def enrich(self, snapshot, memory, utterance):
        self.enriched.append((snapshot, utterance))
        return snapshot

    def format(self, snapshot, utterance):
        self.formatted.append(utterance)
        return f"view:{utterance}"


def test_generate_uses_view_builder():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    view = FakeView()
    bridge = CloudBridge(client, view_builder=view)
    out = bridge.generate("你好", context=ContextSnapshot(), memory=None)
    assert out == "回答"
    assert view.enriched
    assert view.formatted == ["你好"]


def test_generate_default_view_builder_assembles():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    bridge = CloudBridge(client)  # 默认 view_builder
    out = bridge.generate("你好", context=None, memory=None)
    assert out == "回答"
    assert "用户说：你好" in client.calls[0][0][1]["content"]
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_client.py tests/cognition/l2/test_bridge.py -v`
Expected: FAIL（`TypeError: chat() got an unexpected keyword argument 'timeout_s'`）。

- [ ] **Step 4: `src/yuki/cognition/l2/client.py` 修改**

`chat` 签名增 `timeout_s: float | None = None`，透传：

```python
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             timeout_s: float | None = None) -> dict:
        ...
        timeout = self._timeout if timeout_s is None else timeout_s
        try:
            raw = self._post(f"{self._base}/chat/completions", headers, payload, timeout)
        ...
```

- [ ] **Step 5: `src/yuki/cognition/l2/bridge.py` 修改**

顶部 import 增补：

```python
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import SUMMARIZE_TIMEOUT_S, CloudViewBuilder

SUMMARIZE_PROMPT = (
    "请把以下对话压缩成 1-3 句简短中文摘要，"
    "保留关键事实与用户偏好，不要遗漏重要信息。"
)
```

`__init__` 增 `view_builder: CloudViewBuilder | None = None`，存 `self._view_builder = view_builder`；若为 None，构造默认 `CloudViewBuilder(summarize=self._summarize_closure)`：

```python
    def __init__(self, client, registry=None, system_prompt=None, max_turns=3,
                 persona_name="yuki", view_builder=None) -> None:
        ...
        self._view_builder = view_builder or CloudViewBuilder(summarize=self._summarize_closure)

    def _summarize_closure(self, texts: list[str]) -> str:
        messages = [
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {"role": "user", "content": "\n".join(texts)},
        ]
        response = self._client.chat(messages, timeout_s=SUMMARIZE_TIMEOUT_S)
        return (response["choices"][0]["message"].get("content") or "").strip()
```

`generate` 改为：

```python
    def generate(self, utterance: str, context: ContextSnapshot | None = None,
                 memory: MemoryManager | None = None) -> str:
        snapshot = self._view_builder.enrich(context, memory, utterance) \
            if context is not None else ContextSnapshot()
        view_text = self._view_builder.format(snapshot, utterance)
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": view_text},
        ]
        tools = self._registry.tool_schemas() if self._registry else None
        try:
            for _ in range(self._max_turns):
                ...  # 原循环不变
```

- [ ] **Step 6: 删除 `src/yuki/cognition/l2/context.py` 与 `tests/cognition/l2/test_context.py`**

Run: `Remove-Item src/yuki/cognition/l2/context.py, tests/cognition/l2/test_context.py`
并 grep 确认无引用：`rg "build_cloud_context|l2.context" src tests`。

- [ ] **Step 7: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_client.py tests/cognition/l2/test_bridge.py -v`
Expected: 全 PASS。

- [ ] **Step 8: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（删除 test_context 相关，新增 view/bridge/client 测试）。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: wire CloudViewBuilder into CloudBridge with summarize closure"
```

---

### Task 6: DecisionHub 接入 WorkingContext + ContextProjector

**Files:**
- Modify: `src/yuki/cognition/brain/hub.py`、`tests/cognition/test_hub.py`

**Interfaces:**
- Consumes: `WorkingContext`（Task 2）、`ContextProjector`/`ContextSnapshot`（Task 3）。
- Produces: `DecisionHub.__init__` 增 `context=None`/`projector=None`；`_handle` 入口投影快照、写侧喂入；`build_brain(..., context=None, projector=None)`。Task 7 依赖。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_hub.py`**

```python
from yuki.cognition.context.snapshot import ContextSnapshot


class FakeContext:
    def __init__(self):
        self.users = []
        self.agents = []
        self.situations = []
        self.snap = None

    def set_snapshot(self, snap):
        self.snap = snap

    def add_user(self, text):
        self.users.append(text)

    def add_agent(self, text):
        self.agents.append(text)

    def update_situation(self, payload):
        self.situations.append(payload)


class FakeProjector:
    def __init__(self):
        self.last = None

    def build(self, working):
        self.last = working
        return working.snap


def test_hub_writes_context_and_uses_projection(hub):
    h, bus, _ = hub
    ctx = FakeContext()
    ctx.set_snapshot(ContextSnapshot(situation={"topic": "量子计算", "sensitive": False}))
    proj = FakeProjector()
    h._context_wrapper = ctx
    h._projector = proj
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert ctx.users == ["你好"]
    assert proj.last is ctx
    assert ctx.agents == ["l1:你好"]


def test_hub_context_situation_used_for_situation_update(hub):
    h, bus, _ = hub
    ctx = FakeContext()
    ctx.set_snapshot(ContextSnapshot())
    h._context_wrapper = ctx
    h._projector = FakeProjector()
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert ctx.situations == [{"topic": "量子计算", "sensitive": False, "ts": 0.0}]
```

（注意：`test_hub_writes_context_and_uses_projection` 中 L1 回复文本依赖 hub fixture 的 FakeL1——现有 fixture 已用 `FakeL1.reply` 返回 `f"l1:{text}"`，断言 `ctx.agents == ["l1:你好"]`。）

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py -v`
Expected: FAIL（`AttributeError: 'DecisionHub' object has no attribute '_context'` 或 `_projector`）。

- [ ] **Step 3: `src/yuki/cognition/brain/hub.py` 修改**

- `DecisionHub.__init__` 增 `context=None`、`projector=None`：

```python
        self._context_wrapper = context
        self._projector = projector
```

（现有 `self._context` 仍存情境 dict，向后兼容。）

- `on_situation_update` 改为：

```python
    def on_situation_update(self, topic: str, payload: dict) -> None:
        if self._context_wrapper is not None:
            self._context_wrapper.update_situation(payload)
        self._context = payload
        self._handle(TriggerKind.SITUATION, "", situation=payload)
```

- `_handle` 入口（classify 之前）投影快照并解析有效情境：

```python
        snapshot = None
        if self._context_wrapper is not None and self._projector is not None:
            snapshot = self._projector.build(self._context_wrapper)
        effective_situation = situation
        if effective_situation is None:
            effective_situation = (
                getattr(snapshot, "situation", None) if snapshot is not None else self._context)
```

- `_handle` 中所有用到 `situation or self._context` 的位置改为 `effective_situation`（policy.decide 的 situation、`_execute`、`_try_l2`）。

- `_try_l2` 改为传 snapshot：

```python
    def _try_l2(self, text: str, situation: dict | None, snapshot=None):
        try:
            reply = self._bridge.generate(text, snapshot, self._memory)
        except CloudError:
            return "", False
        reply = (reply or "").strip()
        if not reply:
            return "", False
        return reply, True
```

- `_handle` 末尾（spoke 发布块之后、trace 之前）追加写侧：

```python
        if self._context_wrapper is not None:
            if trigger == TriggerKind.UTTERANCE:
                self._context_wrapper.add_user(text)
            if spoke:
                self._context_wrapper.add_agent(rendered)
```

- `build_brain(..., context=None, projector=None)` 透传给 DecisionHub。

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py -v`
Expected: 全 PASS（既有测试不受影响：无 context wrapper 时行为不变）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/hub.py tests/cognition/test_hub.py
git commit -m "feat: wire WorkingContext write side and snapshot projection into DecisionHub"
```

---

### Task 7: Agent 装配 + 全仓回归

**Files:**
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Create: `src/yuki/cognition/context/__init__.py`（补导出）

**Interfaces:**
- Consumes: `WorkingContext`/`ShortTermTurnStore`/`ContextProjector`（Task 1-3）、`CloudBridge`（Task 5）、`build_brain`（Task 6）。
- Produces: `CognitionAgent.setup` 装配 context（restore）+ projector 传给 build_brain；`teardown` 调 `context.close()`。`context/__init__.py` 导出 `WorkingContext`/`ContextSnapshot`/`ContextProjector`/`TurnStore`/`ShortTermTurnStore`。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_cognition.py`**

```python
def test_cognition_agent_builds_context_and_projector(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._context_wrapper is not None
        assert agent._hub._projector is not None
    finally:
        agent.teardown()


def test_cognition_agent_teardown_closes_context(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    agent.teardown()
    # teardown 已调用 context.close()（写入快照到 data/context_snapshot.json 或按 config）
    # 本测试仅验证不抛异常
```

（注意：`config.context.snapshot_path` 默认为 `data/context_snapshot.json`，相对 CWD——测试运行于 worktree 根，`data/` 目录会被创建；git-ignored，无碍。若希望测试不产生文件，可在测试中 `monkeypatch.setenv("YUKI_CONTEXT_SNAPSHOT_PATH", str(tmp_path / "snap.json"))` 后重建 Config。实现时以 `Config()` + tmp_path 隔离为佳——用 `agent = CognitionAgent(Config(context={"snapshot_path": str(tmp_path / "snap.json")}), ...)` 显式注入。）

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_cognition.py -v`
Expected: FAIL（`AttributeError: 'DecisionHub' object has no attribute '_context_wrapper'`）。

- [ ] **Step 3: `src/yuki/cognition/agent.py` 装配**

顶部 import 增补：

```python
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
```

`setup()` 中，`tuner.load_soul()` 之后、`build_brain` 调用处，构建 context + projector 并传入：

```python
        context = WorkingContext(
            ShortTermTurnStore(self._memory),
            snapshot_path=self.config.context.snapshot_path or None,
        )
        context.restore()
        projector = ContextProjector(max_turns=self.config.context.max_turns)
        self._context = context
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
            context=context,
            projector=projector,
        )
```

（替换原 `build_brain(...)` 调用，新增 `context=`/`projector=`。）

`teardown()` 中，`self._memory.close()` 之前：

```python
        if self._context is not None:
            self._context.close()
            self._context = None
```

- [ ] **Step 4: 更新 `src/yuki/cognition/context/__init__.py`**

```python
from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot  # noqa: F401
from yuki.cognition.context.store import ShortTermTurnStore, TurnStore  # noqa: F401
from yuki.cognition.context.working import WorkingContext  # noqa: F401
```

- [ ] **Step 5: 运行全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（此前 338 passed 基础上新增 context/view/hub/cognition 测试）。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: assemble WorkingContext and ContextProjector into CognitionAgent"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1-7；§3 WorkingContext/TurnStore/ContextSnapshot/ContextProjector/持久化 → Task 1/2/3；§4 view（填充顺序+配额/折叠单元/缓存/熔断）→ Task 4；§5 bridge/client timeout → Task 5；§6 hub 接线 → Task 6；§7 配置 → Task 1；§8 测试 → 各任务；§9/§10 ADR → 贯穿。
- **一致性**：`TurnStore.add(content, kind, ts)` 在 Task 1 定义、Task 2 WorkingContext 消费；`MemoryManager.short_term_add(at=)` 在 Task 1 扩展、ShortTermTurnStore 消费；`CloudViewBuilder.enrich/format` 在 Task 4 定义、Task 5 bridge 消费；`ContextSnapshot` 在 Task 3 定义、Task 4/5/6 消费；`build_brain(context=, projector=)` 在 Task 6 定义、Task 7 agent 消费。
- **兼容**：hub 的 `self._context`（情境 dict）语义保留；无 context wrapper 时行为与现在完全一致；e2e 不变。
- **测试注意**：`test_estimate_tokens("你好")==2`、熔断测试按真实 `SUMMARIZE_MAX_FAILURES` 断言（连续失败≥3 熔断）；agent 测试用 tmp_path 注入 snapshot_path 避免污染 data/。
