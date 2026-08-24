# 持久化原子性 Implementation Plan（架构评审主题 1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `SoulStore` 已验证的 tmpfile+rename 原子写模式推广到 `PersonaStore` 与 `WorkingContext`，消除写入崩溃导致半写文件、数据永久丢失的风险。

**Architecture:** 从 `src/yuki/cognition/brain/soul.py` 提取 `_atomic_write_json` 为共享工具 `src/yuki/persistence.py::atomic_write_json`，三个 Store（SoulStore/PersonaStore/WorkingContext）统一调用。行为契约保持等价：写目标父目录自动创建、tmp 写入 + `os.replace`、`OSError` 由调用方吞掉记 warning。

**Tech Stack:** Python ≥3.11，pytest。无新增运行时依赖。

## Global Constraints

- `atomic_write_json(path: Path, payload: dict) -> None` 语义必须与现 `_atomic_write_json` 完全一致（soul.py:82-86）：`path.parent.mkdir(parents=True, exist_ok=True)` → `path.with_suffix(suffix+".tmp")` → `write_text(json.dumps(..., ensure_ascii=False, indent=2), encoding="utf-8")` → `os.replace(tmp, path)`。
- 所有 Store 的写失败仍是 best-effort：`OSError` 在各自 `try/except` 内吞掉并 `logger.warning`，不向上抛、不影响内存态。
- 不放宽任何磁盘写入策略；不加 fsync（与现有 SoulStore 一致，目标是对抗半写文件而非断电持久化）。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**新增**
- `src/yuki/persistence.py` — `atomic_write_json` 共享工具（顶层共享工具，遵循 AGENTS.md：共享工具直接放 `src/yuki/`，无 `utils/` 包）
- `tests/test_persistence.py`

**修改**
- `src/yuki/cognition/brain/soul.py` — 删除本地 `_atomic_write_json`，改用共享工具（2 处调用）
- `src/yuki/cognition/brain/snapshots.py` — `_persist()` 用共享工具
- `src/yuki/cognition/context/working.py` — `snapshot()` 用共享工具
- 测试：`tests/cognition/test_snapshots.py`、`tests/cognition/context/test_working.py`

---

### Task 1: 提取共享原子写工具

**Files:**
- Create: `src/yuki/persistence.py`
- Create: `tests/test_persistence.py`

**Interfaces:**
- Consumes: 无。
- Produces: `atomic_write_json(path: Path, payload: dict) -> None`。Task 2/3/4 依赖此函数。

- [ ] **Step 1: 创建 `tests/test_persistence.py`（先红）**

```python
import json

import pytest

from yuki.persistence import atomic_write_json


def test_atomic_write_json_roundtrip_creates_parent(tmp_path):
    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1, "b": [2, 3]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_atomic_write_json_overwrites_existing(tmp_path):
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_atomic_write_json_keeps_target_intact_when_rename_crashes(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def boom_replace(*args, **kwargs):
        raise OSError("crash before rename")

    monkeypatch.setattr("yuki.persistence.os.replace", boom_replace)
    with pytest.raises(OSError):
        atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.persistence'`）。

- [ ] **Step 3: 创建 `src/yuki/persistence.py`**

```python
import json
import os
from pathlib import Path


def atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 JSON：写 .tmp 后 os.replace，目标文件永不见半写内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/persistence.py tests/test_persistence.py
git commit -m "feat: extract atomic_write_json shared persistence util"
```

---

### Task 2: SoulStore/TunerStateStore 迁移到共享工具

**Files:**
- Modify: `src/yuki/cognition/brain/soul.py`
- Test: `tests/cognition/test_soul.py`

**Interfaces:**
- Consumes: `atomic_write_json`（Task 1）。
- Produces: `soul.py` 不再定义 `_atomic_write_json`；`TunerStateStore.save` 与 `SoulStore.save` 改调用共享函数。行为等价。

- [ ] **Step 1: 删除本地 `_atomic_write_json` 并导入共享工具**

在 `src/yuki/cognition/brain/soul.py`：
- 删除第 82-86 行 `_atomic_write_json` 定义。
- 在 import 区新增：`from yuki.persistence import atomic_write_json`（与 `from yuki.logger import get_logger` 相邻）。
- 替换两处调用：`TunerStateStore.save`（原 soul.py:123）与 `SoulStore.save`（原 soul.py:209）的 `_atomic_write_json(self._path, payload)` → `atomic_write_json(self._path, payload)`。

- [ ] **Step 2: 运行 soul 相关测试**

Run: `python -m pytest tests/cognition/test_soul.py tests/cognition/test_tuner.py tests/cognition/test_sedimenter.py -v`
Expected: 全 PASS（行为等价，无需改断言）。

- [ ] **Step 3: Commit**

```bash
git add src/yuki/cognition/brain/soul.py
git commit -m "refactor: route soul/tuner persistence through shared atomic writer"
```

---

### Task 3: PersonaStore 原子持久化

**Files:**
- Modify: `src/yuki/cognition/brain/snapshots.py`
- Test: `tests/cognition/test_snapshots.py`

**Interfaces:**
- Consumes: `atomic_write_json`（Task 1）。
- Produces: `PersonaStore._persist()` 原子写，失败仍吞 OSError + warning。`save/rollback/lock/reset/import_snapshot` 的磁盘行为不变。

- [ ] **Step 1: 追加崩溃测试到 `tests/cognition/test_snapshots.py`**

```python
def test_persist_is_atomic_under_crash(tmp_path, monkeypatch):
    store = make(tmp_path)
    store.save("v1", {})
    before = (tmp_path / "snapshots.json").read_text(encoding="utf-8")

    def boom_replace(*args, **kwargs):
        raise OSError("crash before rename")

    monkeypatch.setattr("yuki.persistence.os.replace", boom_replace)
    store.save("v2", {})  # _persist 内部失败被吞，不崩溃

    assert (tmp_path / "snapshots.json").read_text(encoding="utf-8") == before
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_snapshots.py::test_persist_is_atomic_under_crash -v`
Expected: FAIL——当前 `_persist` 直接 `write_text` 覆盖，崩溃模拟下目标文件已被 v2 覆盖（断言 `== before` 失败）。

- [ ] **Step 3: 改写 `snapshots.py::_persist`**

在 `src/yuki/cognition/brain/snapshots.py`：
- 新增 import：`from yuki.persistence import atomic_write_json`
- `_persist` 改为：

```python
def _persist(self) -> None:
    try:
        payload = {
            "persona_name": self._persona_name,
            "active": self._active,
            "versions": [self._versions[k] for k in sorted(self._versions)],
        }
        atomic_write_json(self._path, payload)
    except OSError as exc:
        logger.warning("persona store save failed", error=str(exc))
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_snapshots.py -v`
Expected: 全 PASS（含新增崩溃测试）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/snapshots.py tests/cognition/test_snapshots.py
git commit -m "fix: atomic persist for PersonaStore snapshot history"
```

---

### Task 4: WorkingContext 原子快照

**Files:**
- Modify: `src/yuki/cognition/context/working.py`
- Test: `tests/cognition/context/test_working.py`

**Interfaces:**
- Consumes: `atomic_write_json`（Task 1）。
- Produces: `WorkingContext.snapshot()` 原子写；`restore()` 行为不变。失败仍吞 OSError + warning。

- [ ] **Step 1: 追加崩溃测试到 `tests/cognition/context/test_working.py`**

```python
def test_snapshot_is_atomic_under_crash(tmp_path, monkeypatch):
    manager = make_store(tmp_path)
    path = tmp_path / "snap.json"
    ctx = WorkingContext(manager, snapshot_path=path)
    ctx.add_user("第一轮")
    ctx.close()  # 触发 snapshot
    before = path.read_text(encoding="utf-8")

    def boom_replace(*args, **kwargs):
        raise OSError("crash before rename")

    monkeypatch.setattr("yuki.persistence.os.replace", boom_replace)
    ctx2 = WorkingContext(manager, snapshot_path=path)
    ctx2.add_user("第二轮")
    ctx2.close()  # snapshot 内部失败被吞，不崩溃

    assert path.read_text(encoding="utf-8") == before
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/context/test_working.py::test_snapshot_is_atomic_under_crash -v`
Expected: FAIL——当前 `snapshot()` 直接 `write_text` 覆盖，崩溃模拟下文件已被第二轮覆盖。

- [ ] **Step 3: 改写 `working.py::snapshot`**

在 `src/yuki/cognition/context/working.py`：
- 新增 import：`from yuki.persistence import atomic_write_json`
- `snapshot` 方法体改 `self._snapshot_path.write_text(...)`（原 working.py:61-62）为：

```python
    def snapshot(self) -> None:
        if self._snapshot_path is None:
            return
        try:
            payload = {
                "turns": [{"content": t["content"], "kind": t["kind"], "ts": t["ts"]}
                          for t in reversed(self._store.items())],
                "situation": self._situation,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            atomic_write_json(self._snapshot_path, payload)
        except OSError as exc:
            logger.warning("context snapshot failed", error=str(exc))
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/context/test_working.py tests/cognition/context/test_snapshot.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/context/working.py tests/cognition/context/test_working.py
git commit -m "fix: atomic snapshot for WorkingContext conversation context"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 1 三目标全部覆盖——共享工具（Task 1）、SoulStore 复用（Task 2）、PersonaStore（Task 3）、WorkingContext（Task 4）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `atomic_write_json(path, payload)` 在 Task 1 定义、Task 2/3/4 同名调用；崩溃测试统一 monkeypatch `yuki.persistence.os.replace`（跨模块调用点一致）。
- **行为等价：** 三个 Store 的写失败均保持 best-effort 吞 OSError，符合现有契约。
