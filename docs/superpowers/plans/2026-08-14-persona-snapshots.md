# Yuki 环3 人格快照 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现环3 人格快照——`PersonaGenerator`（规则组装 + 可选 LLM 精修）+ `PersonaStore`（生成即版本：cap/锁/跳过/回滚/导出）+ 命令行管理；CloudBridge 用 active 快照，agent 装配并在环2 沉淀后刷新。

**Architecture:** `brain/persona.py`（纯生成）、`brain/snapshots.py`（版本存储）、`persona_cli.py`（管理工具）；`config.persona` 节承载基础人格描述（自 CloudBridge 常量迁出）；agent 装配 store+generator，会话初始化与环2 沉淀回调时刷新 persona 并更新 CloudBridge 系统提示。

**Tech Stack:** Python ≥3.11，stdlib json/time/difflib/argparse + 现有 pydantic。零新增运行时依赖。

## Global Constraints

- 零新增运行时依赖；零协议变更（REPLY 主题/载荷不变）。
- `format_preferences(preferences: list[dict]) -> str`：空 → ""；否则"用户偏好：\n- {content}\n..."。
- `format_soul_params(params: dict) -> str`：空 → ""；否则"参数说明：\n- {k}: {v}\n..."。
- `generate(persona_name, preferences, soul_params, base_prompt=None, refine=None) -> str`：base 注入 `{persona}`；prefs/params 非空则追加段；`refine` 非 None 时调 `refine(text)`，成功且非空则返回精修结果，异常/空回退规则结果。
- `PersonaStore(path, *, max_versions=50, persona_name="yuki")`：
  - `save(prompt, params) -> PersonaSnapshot | None`：与 active 完全相同 → None（跳过）；否则新版本号递增、设 active、`_prune`（超 cap 删最旧**非锁定**；**v1 永远保留**）、持久化。
  - `active()/list_versions()/rollback(v)/lock(v)/reset()/diff(v1,v2)/export(v)/import_snapshot(data)`；未知版本 `ValueError`；损坏文件 → 空。
  - 持久化 `{persona_name, active, versions}`；写失败仅告警。
- `PersonaSnapshot(version, persona_prompt, params, created_at, locked)` frozen。
- `persona_cli.py`：`python -m yuki.persona_cli` 子命令 `list/active/rollback/lock/reset/diff/export/import`（`--path`/`--max-versions`）；未知版本 → 退出 1。
- `CloudBridge.__init__(..., system_prompt=None, persona_name="yuki")`：提供的 `system_prompt` **原样使用**（不再 `.format`）；`set_system_prompt(text)` 更新。
- `PreferenceSedimenter` 增 `on_sedimented: Callable[[], None] | None`，`_write_preference` 末尾调用。
- `Config` 增 `persona:` 节（prompt 默认=原 DEFAULT_PERSONA_PROMPT 文本/max_versions=50/enable_llm_refine=false/snapshots_path="data/persona_snapshots.json"，env `YUKI_PERSONA_*`）。
- `CognitionAgent.setup`：装配 `PersonaStore` + 生成器；会话初始化 `persona_refresh()`（读 preference 记忆过滤 `sensitivity != 2` → generate → store.save → 有新快照则 `bridge.set_system_prompt`）；`sedimenter.on_sedimented = persona_refresh`。
- e2e 等价：默认无偏好变化 → 跳过相同 → 不产生版本文件；awake → `我在,你说。` 不变。
- 测试命令（仓库根）：`& ".venv\Scripts\python.exe" -m pytest <文件> -v`；全仓 `-m pytest`。
- 设计文档：`docs/superpowers/specs/2026-08-14-persona-snapshots-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/persona.py`
- `src/yuki/cognition/brain/snapshots.py`
- `src/yuki/persona_cli.py`
- `tests/cognition/test_persona.py`、`tests/cognition/test_snapshots.py`、`tests/test_persona_cli.py`

**修改**
- `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`（persona 节）
- `src/yuki/cognition/l2/bridge.py`、`tests/cognition/l2/test_bridge.py`（system_prompt 原样 + set_system_prompt）
- `src/yuki/cognition/brain/sedimenter.py`、`tests/cognition/test_sedimenter.py`（on_sedimented 回调）
- `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`（装配 + 刷新）

---

### Task 1: persona 配置 + PersonaGenerator

**Files:**
- Create: `src/yuki/cognition/brain/persona.py`
- Modify: `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- Test: `tests/cognition/test_persona.py`

**Interfaces:**
- Consumes: 无。
- Produces: `format_preferences`/`format_soul_params`/`generate`（§Global Constraints）；`Config.persona`（PersonaConfig）。Task 2/4 依赖。

- [ ] **Step 1: 追加 persona 配置测试到 `tests/test_config.py`**

```python
def test_persona_defaults():
    config = Config()
    assert config.persona.max_versions == 50
    assert config.persona.enable_llm_refine is False
    assert config.persona.snapshots_path == "data/persona_snapshots.json"
    assert "yuki" in config.persona.prompt or "{persona}" in config.persona.prompt


def test_persona_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_PERSONA_MAX_VERSIONS", "100")
    monkeypatch.setenv("YUKI_PERSONA_ENABLE_LLM_REFINE", "true")
    config = Config.load(None)
    assert config.persona.max_versions == 100
    assert config.persona.enable_llm_refine is True
```

- [ ] **Step 2: 写失败测试 `tests/cognition/test_persona.py`**

```python
from yuki.cognition.brain.persona import format_preferences, format_soul_params, generate


def test_format_preferences_empty():
    assert format_preferences([]) == ""


def test_format_preferences_templated():
    out = format_preferences([{"content": "用户喜欢安静"}, {"content": "回复要简短"}])
    assert "用户偏好：" in out
    assert "- 用户喜欢安静" in out
    assert "- 回复要简短" in out


def test_format_soul_params_empty():
    assert format_soul_params({}) == ""


def test_format_soul_params_templated():
    out = format_soul_params({"humor": "high"})
    assert "humor: high" in out


def test_generate_assembles_sections():
    out = generate("yuki", [{"content": "喜欢猫"}], {"cooldown": 120},
                   base_prompt="你是{persona},温柔。")
    assert out.startswith("你是yuki,温柔。")
    assert "用户偏好：" in out
    assert "参数说明：" in out


def test_generate_omits_empty_sections():
    out = generate("yuki", [], {}, base_prompt="你是{persona}。")
    assert "用户偏好" not in out
    assert "参数说明" not in out


def test_generate_refine_success():
    out = generate("yuki", [], {}, base_prompt="base", refine=lambda text: "精修后的文本")
    assert out == "精修后的文本"


def test_generate_refine_failure_falls_back():
    def boom(text):
        raise RuntimeError("down")

    out = generate("yuki", [], {}, base_prompt="base", refine=boom)
    assert out == "base"
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_persona.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.persona'`）。

- [ ] **Step 4: 创建 `src/yuki/cognition/brain/persona.py`**

```python
from typing import Callable

DEFAULT_BASE_PROMPT = (
    "你是{persona},一个温柔的中文语音陪伴 agent。"
    "回复简短自然(1-3 句),贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
    "可以用工具查询记忆,但不要捏造记忆内容。"
)


def format_preferences(preferences: list[dict]) -> str:
    if not preferences:
        return ""
    lines = ["用户偏好："]
    for p in preferences:
        lines.append(f"- {p.get('content', '')}")
    return "\n".join(lines)


def format_soul_params(params: dict) -> str:
    if not params:
        return ""
    lines = ["参数说明："]
    for key, value in params.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def generate(persona_name: str, preferences: list[dict], soul_params: dict,
             base_prompt: str | None = None,
             refine: Callable[[str], str] | None = None) -> str:
    base = (base_prompt or DEFAULT_BASE_PROMPT).format(persona=persona_name)
    prefs = format_preferences(preferences)
    params = format_soul_params(soul_params)
    text = base
    if prefs:
        text += "\n\n" + prefs
    if params:
        text += "\n\n" + params
    if refine is not None:
        try:
            refined = refine(text)
            if refined and refined.strip():
                return refined.strip()
        except Exception:
            pass  # 精修失败回退规则结果
    return text
```

- [ ] **Step 5: `src/yuki/config.py` 加 PersonaConfig 并注册**

在 `SedimenterConfig` 之后新增：

```python
class PersonaConfig(BaseModel):
    prompt: str = (
        "你是{persona},一个温柔的中文语音陪伴 agent。"
        "回复简短自然(1-3 句),贴合陪伴场景。"
        "不替用户操作系统或浏览器。"
        "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
        "可以用工具查询记忆,但不要捏造记忆内容。"
    )
    max_versions: int = Field(50, ge=1)
    enable_llm_refine: bool = False
    snapshots_path: str = "data/persona_snapshots.json"
```

在 `Config` 中 `sedimenter` 字段之后新增：

```python
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
```

在 `Config.load` 的 section 元组中 `("sedimenter", SedimenterConfig),` 之后新增：

```python
            ("persona", PersonaConfig),
```

- [ ] **Step 6: `config.example.yaml` 加 persona 节**

```yaml
persona:
  prompt: "你是{persona},一个温柔的中文语音陪伴 agent。回复简短自然(1-3 句),贴合陪伴场景。不替用户操作系统或浏览器。用户提到自伤/自杀等危机时,优先表达关怀并建议求助。可以用工具查询记忆,但不要捏造记忆内容。"
  max_versions: 50
  enable_llm_refine: false
  snapshots_path: data/persona_snapshots.json
```

- [ ] **Step 7: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_persona.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/cognition/brain/persona.py src/yuki/config.py config.example.yaml tests/cognition/test_persona.py tests/test_config.py
git commit -m "feat: add persona generator and persona config"
```

---

### Task 2: PersonaStore（版本历史）

**Files:**
- Create: `src/yuki/cognition/brain/snapshots.py`
- Test: `tests/cognition/test_snapshots.py`

**Interfaces:**
- Consumes: 无。
- Produces: `PersonaSnapshot`（frozen）、`PersonaStore`（§Global Constraints）。Task 3/4 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_snapshots.py`**

```python
import pytest

from yuki.cognition.brain.snapshots import PersonaStore


def make(tmp_path, **kwargs):
    return PersonaStore(tmp_path / "snapshots.json", **kwargs)


def test_save_creates_active_and_increments(tmp_path):
    store = make(tmp_path, max_versions=10)
    s1 = store.save("prompt1", {"cooldown": 120})
    s2 = store.save("prompt2", {"cooldown": 140})
    assert s1.version == 1
    assert s2.version == 2
    assert store.active().version == 2
    assert [v.version for v in store.list_versions()] == [1, 2]


def test_save_skips_identical(tmp_path):
    store = make(tmp_path)
    store.save("same", {"a": 1})
    assert store.save("same", {"a": 1}) is None
    assert len(store.list_versions()) == 1


def test_cap_prunes_oldest_non_locked_keeps_v1(tmp_path):
    store = make(tmp_path, max_versions=3)
    store.save("v1", {})
    store.save("v2", {})
    store.save("v3", {})
    store.lock(2)
    store.save("v4", {})   # 超 cap=3 → 删最旧非锁定（v1 保留，v3 删）
    versions = {v.version for v in store.list_versions()}
    assert 1 in versions
    assert 2 in versions
    assert 4 in versions
    assert 3 not in versions


def test_rollback_and_reset(tmp_path):
    store = make(tmp_path)
    store.save("a", {})
    store.save("b", {})
    store.rollback(1)
    assert store.active().persona_prompt == "a"
    store.reset()
    assert store.active() is None or store.active().version == 1
    assert len(store.list_versions()) <= 1


def test_lock_exempts_from_prune(tmp_path):
    store = make(tmp_path, max_versions=2)
    store.save("v1", {})
    store.save("v2", {})
    store.lock(1)
    store.save("v3", {})   # 超 cap=2 → v1 锁定保留、v2 删
    versions = {v.version for v in store.list_versions()}
    assert versions == {1, 3}


def test_diff_and_export_import(tmp_path):
    store = make(tmp_path)
    store.save("line1\nline2", {})
    store.save("line1\nCHANGED", {})
    diff = store.diff(1, 2)
    assert "CHANGED" in diff
    data = store.export(1)
    store2 = make(tmp_path)
    store2.import_snapshot(data)
    assert store2.active() is None  # 导入不自动设 active
    assert any(v.version == 1 for v in store2.list_versions())


def test_unknown_version_raises(tmp_path):
    store = make(tmp_path)
    with pytest.raises(ValueError):
        store.rollback(99)
    with pytest.raises(ValueError):
        store.lock(99)


def test_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "snapshots.json"
    path.write_text("{broken", encoding="utf-8")
    store = PersonaStore(path)
    assert store.active() is None
    assert store.list_versions() == []
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_snapshots.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.snapshots'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/snapshots.py`**

```python
import difflib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.snapshots")


@dataclass(frozen=True)
class PersonaSnapshot:
    version: int
    persona_prompt: str
    params: dict
    created_at: float
    locked: bool = False


class PersonaStore:
    """人格快照版本历史：生成即版本、跳过相同、cap 清理（锁定豁免、v1 保留）、回滚/重置/导出。"""

    def __init__(self, path: str | Path, *, max_versions: int = 50,
                 persona_name: str = "yuki") -> None:
        self._path = Path(path)
        self._max_versions = max_versions
        self._persona_name = persona_name
        self._versions: dict[int, dict] = {}
        self._active: int | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("persona store load failed")
            return
        for v in data.get("versions") or []:
            if isinstance(v, dict) and isinstance(v.get("version"), int):
                self._versions[v["version"]] = v
        self._active = data.get("active")

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "persona_name": self._persona_name,
                "active": self._active,
                "versions": [self._versions[k] for k in sorted(self._versions)],
            }
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("persona store save failed", error=str(exc))

    def _as_snapshot(self, v: dict) -> PersonaSnapshot:
        return PersonaSnapshot(
            version=v["version"],
            persona_prompt=v["persona_prompt"],
            params=v.get("params", {}),
            created_at=v.get("created_at", 0.0),
            locked=bool(v.get("locked", False)),
        )

    def active(self) -> PersonaSnapshot | None:
        if self._active is None:
            return None
        v = self._versions.get(self._active)
        return self._as_snapshot(v) if v else None

    def list_versions(self) -> list[PersonaSnapshot]:
        return [self._as_snapshot(self._versions[k]) for k in sorted(self._versions)]

    def save(self, persona_prompt: str, params: dict) -> PersonaSnapshot | None:
        current = self.active()
        if current is not None and current.persona_prompt == persona_prompt and current.params == params:
            return None  # 跳过相同
        version = (max(self._versions) if self._versions else 0) + 1
        self._versions[version] = {
            "version": version, "persona_prompt": persona_prompt, "params": params,
            "created_at": time.time(), "locked": False,
        }
        self._active = version
        self._prune()
        self._persist()
        return self._as_snapshot(self._versions[version])

    def _prune(self) -> None:
        removable = [v for v in sorted(self._versions)
                     if v != 1 and not self._versions[v].get("locked")]
        while len(self._versions) > self._max_versions and removable:
            oldest = removable.pop(0)
            del self._versions[oldest]
            if self._active == oldest:
                self._active = min(self._versions) if self._versions else None

    def rollback(self, version: int) -> None:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        self._active = version
        self._persist()

    def lock(self, version: int) -> None:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        self._versions[version]["locked"] = True
        self._persist()

    def reset(self) -> None:
        keep = {1: self._versions[1]} if 1 in self._versions else {}
        self._versions = keep
        self._active = 1 if 1 in self._versions else None
        self._persist()

    def diff(self, v1: int, v2: int) -> str:
        a = self._versions[v1]["persona_prompt"].splitlines()
        b = self._versions[v2]["persona_prompt"].splitlines()
        return "\n".join(difflib.unified_diff(a, b, fromfile=f"v{v1}", tofile=f"v{v2}"))

    def export(self, version: int) -> dict:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        return dict(self._versions[version])

    def import_snapshot(self, data: dict) -> None:
        if not isinstance(data.get("persona_prompt"), str) or not isinstance(data.get("version"), int):
            raise ValueError("invalid snapshot")
        version = data["version"]
        self._versions[version] = {
            "version": version,
            "persona_prompt": data["persona_prompt"],
            "params": data.get("params", {}),
            "created_at": data.get("created_at", time.time()),
            "locked": bool(data.get("locked", False)),
        }
        self._persist()
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_snapshots.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/snapshots.py tests/cognition/test_snapshots.py
git commit -m "feat: add PersonaStore with version history, cap, lock, rollback"
```

---

### Task 3: Persona CLI

**Files:**
- Create: `src/yuki/persona_cli.py`
- Test: `tests/test_persona_cli.py`

**Interfaces:**
- Consumes: `PersonaStore`（Task 2）。
- Produces: `python -m yuki.persona_cli`（`--path`/`--max-versions` + `list/active/rollback/lock/reset/diff/export/import`）；未知版本退出 1。

- [ ] **Step 1: 写失败测试 `tests/test_persona_cli.py`**

```python
import json

from yuki.cognition.brain.snapshots import PersonaStore
from yuki.persona_cli import main


def test_cli_list_and_active(tmp_path, capsys):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("你好呀", {"cooldown": 120})
    assert main(["--path", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "v1" in out
    assert main(["--path", str(db), "active"]) == 0
    assert "你好呀" in capsys.readouterr().out


def test_cli_rollback_lock_reset(tmp_path):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("a", {})
    store.save("b", {})
    assert main(["--path", str(db), "rollback", "1"]) == 0
    assert PersonaStore(db).active().persona_prompt == "a"
    assert main(["--path", str(db), "lock", "1"]) == 0
    assert main(["--path", str(db), "reset"]) == 0
    assert len(PersonaStore(db).list_versions()) <= 1


def test_cli_diff_and_export_import(tmp_path, capsys):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("line1\nline2", {})
    store.save("line1\nCHANGED", {})
    assert main(["--path", str(db), "diff", "1", "2"]) == 0
    assert "CHANGED" in capsys.readouterr().out
    assert main(["--path", str(db), "export", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    imported = tmp_path / "imp.json"
    (tmp_path / "dump.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert main(["--path", str(imported), "import", str(tmp_path / "dump.json")]) == 0
    assert any(v.version == 1 for v in PersonaStore(imported).list_versions())


def test_cli_unknown_version_errors(tmp_path):
    db = tmp_path / "snap.json"
    PersonaStore(db).save("x", {})
    assert main(["--path", str(db), "rollback", "99"]) == 1
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_persona_cli.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.persona_cli'`）。

- [ ] **Step 3: 创建 `src/yuki/persona_cli.py`**

```python
import argparse
import json
import sys

from yuki.cognition.brain.snapshots import PersonaStore


def build_store(args) -> PersonaStore:
    return PersonaStore(args.path, max_versions=args.max_versions)


def _cmd_list(args, store):
    for snap in store.list_versions():
        mark = " [locked]" if snap.locked else ""
        print(f"v{snap.version}{mark} :: {snap.persona_prompt[:40]}")
    active = store.active()
    print(f"active: v{active.version if active else 'none'}")


def _cmd_active(args, store):
    snap = store.active()
    if snap is None:
        print("no active snapshot", file=sys.stderr)
        return 1
    print(snap.persona_prompt)
    return 0


def _cmd_rollback(args, store):
    store.rollback(args.version)
    print(f"rolled back to v{args.version}")


def _cmd_lock(args, store):
    store.lock(args.version)
    print(f"locked v{args.version}")


def _cmd_reset(args, store):
    store.reset()
    print("reset to base snapshot")


def _cmd_diff(args, store):
    print(store.diff(args.v1, args.v2))


def _cmd_export(args, store):
    print(json.dumps(store.export(args.version), ensure_ascii=False, indent=2))


def _cmd_import(args, store):
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    store.import_snapshot(data)
    print(f"imported v{data['version']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yuki.persona", description="Yuki persona snapshots admin")
    parser.add_argument("--path", default="data/persona_snapshots.json")
    parser.add_argument("--max-versions", type=int, default=50)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list"); p.set_defaults(func=_cmd_list)
    p = sub.add_parser("active"); p.set_defaults(func=_cmd_active)
    p = sub.add_parser("rollback"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_rollback)
    p = sub.add_parser("lock"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_lock)
    p = sub.add_parser("reset"); p.set_defaults(func=_cmd_reset)
    p = sub.add_parser("diff"); p.add_argument("v1", type=int); p.add_argument("v2", type=int); p.set_defaults(func=_cmd_diff)
    p = sub.add_parser("export"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_export)
    p = sub.add_parser("import"); p.add_argument("file"); p.set_defaults(func=_cmd_import)

    args = parser.parse_args(argv)
    store = build_store(args)
    try:
        return args.func(args, store) or 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_persona_cli.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/persona_cli.py tests/test_persona_cli.py
git commit -m "feat: add persona snapshots CLI"
```

---

### Task 4: CloudBridge active persona + sedimenter 回调 + agent 装配 + 回归

**Files:**
- Modify: `src/yuki/cognition/l2/bridge.py`、`tests/cognition/l2/test_bridge.py`
- Modify: `src/yuki/cognition/brain/sedimenter.py`、`tests/cognition/test_sedimenter.py`
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Test: 各文件

**Interfaces:**
- Consumes: `PersonaGenerator`（Task 1）、`PersonaStore`（Task 2）、`CloudBridge`、`PreferenceSedimenter`。
- Produces: `CloudBridge.set_system_prompt`；`PreferenceSedimenter.on_sedimented` 回调；`CognitionAgent` 装配 + `persona_refresh`。全仓回归。

- [ ] **Step 1: 追加 bridge 测试到 `tests/cognition/l2/test_bridge.py`**

```python
def test_generate_uses_provided_system_prompt_as_is():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    bridge = CloudBridge(client, system_prompt="你好呀{persona}保持这样")  # 不做 .format
    bridge.generate("你好", context=None, memory=None)
    assert client.calls[0][0][0]["content"] == "你好呀{persona}保持这样"


def test_set_system_prompt_updates():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    bridge = CloudBridge(client, system_prompt="初始")
    bridge.set_system_prompt("新的系统提示")
    bridge.generate("你好", context=None, memory=None)
    assert client.calls[0][0][0]["content"] == "新的系统提示"
```

- [ ] **Step 2: 追加 sedimenter 测试到 `tests/cognition/test_sedimenter.py`**

```python
def test_on_sedimented_callback_fires_on_write(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    fired = []
    sed = PreferenceSedimenter(memory, min_signals=1, on_sedimented=lambda: fired.append(1))
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert fired  # 沉淀即回调
```

- [ ] **Step 3: 追加 agent 测试到 `tests/cognition/test_cognition.py`**

```python
def test_cognition_agent_assembles_persona(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")},
               context={"snapshot_path": str(tmp_path / "ctx.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._persona_store is not None
        assert agent._hub._sedimenter._on_sedimented is not None
    finally:
        agent.teardown()
```

- [ ] **Step 4: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_bridge.py tests/cognition/test_sedimenter.py tests/cognition/test_cognition.py -v`
Expected: FAIL（`AttributeError: 'CloudBridge' object has no attribute 'set_system_prompt'` / `TypeError: ... on_sedimented`）。

- [ ] **Step 5: `src/yuki/cognition/l2/bridge.py` 修改**

`__init__` 中系统提示改为原样使用（不再 `.format`）：

```python
        self._system = system_prompt or DEFAULT_PERSONA_PROMPT.format(persona=persona_name)
```

新增：

```python
    def set_system_prompt(self, text: str) -> None:
        self._system = text
```

（`DEFAULT_PERSONA_PROMPT` 常量保留为兜底；`persona_name` 仅用于该兜底的 `.format`。）

- [ ] **Step 6: `src/yuki/cognition/brain/sedimenter.py` 修改**

`__init__` 增 `on_sedimented: Callable[[], None] | None = None`，存 `self._on_sedimented`；`_write_preference` 末尾调用：

```python
        if self._on_sedimented is not None:
            self._on_sedimented()
```

（顶部 `from typing import Callable`。）

- [ ] **Step 7: `src/yuki/cognition/agent.py` 装配**

import 增补：

```python
from yuki.cognition.brain.persona import generate as generate_persona
from yuki.cognition.brain.snapshots import PersonaStore
```

`setup()` 中，构建 `self._persona_store` + `persona_refresh` 闭包，装配 `sedimenter` 后设置回调，构建 bridge 系统提示：

```python
        self._persona_store = PersonaStore(
            self.config.persona.snapshots_path,
            max_versions=self.config.persona.max_versions,
            persona_name=self.config.persona_name,
        )

        def persona_refresh() -> None:
            prefs = [m for m in self._memory.list(memory_type="preference")
                     if m.get("sensitivity", 0) != 2]
            prompt = generate_persona(
                self.config.persona_name, prefs, {},
                base_prompt=self.config.persona.prompt,
            )
            snap = self._persona_store.save(prompt, {})
            if snap is not None and bridge is not None:
                bridge.set_system_prompt(snap.persona_prompt)
        self._persona_refresh = persona_refresh

        # 装配 sedimenter（含回调）后构建 bridge
```

装配顺序调整为：先建 `bridge`，再建 `persona_store` 与 `persona_refresh`，随后 `sedimenter = PreferenceSedimenter(..., on_sedimented=persona_refresh)`，然后 `build_brain(...)`；最后调一次 `persona_refresh()`（会话初始化）。若 bridge 未启用（cloud 关），`persona_refresh` 仍更新 store（快照历史独立于 cloud）；若 bridge 存在，把 active 快照 prompt（或 config 基础）设为系统提示：

```python
        active = self._persona_store.active()
        if bridge is not None:
            bridge.set_system_prompt(active.persona_prompt if active
                                     else self.config.persona.prompt.format(persona=self.config.persona_name))
        persona_refresh()
```

（实现时按 agent.py 实际装配顺序组织；`self._persona_refresh`/`self._persona_store` 在 `__init__` 置 `None` 供 teardown 安全。）

`teardown()` 无需额外关闭（PersonaStore 无资源句柄）。

- [ ] **Step 8: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_bridge.py tests/cognition/test_sedimenter.py tests/cognition/test_cognition.py tests/cognition/test_persona.py tests/cognition/test_snapshots.py tests/test_persona_cli.py -v`
Expected: 全 PASS。

- [ ] **Step 9: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（此前 400 passed 基础上新增 persona 相关测试）。

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: wire active persona snapshot into CloudBridge and CognitionAgent"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1-4；§3 PersonaGenerator → Task 1；§4 PersonaStore（cap/锁/跳过/回滚/reset/diff/导出）→ Task 2；§4 偏好来源/过滤 → Task 4；§5 消费（bridge active）→ Task 4；§6 配置 → Task 1；§7 测试 → 各任务；§8/§9 ADR → 贯穿。
- **一致性**：`generate`（Task 1）在 Task 4 agent 装配消费；`PersonaStore.save`（Task 2）在 Task 3 CLI 与 Task 4 装配消费；`CloudBridge.set_system_prompt`/`sedimenter.on_sedimented`（Task 4）衔接；`PersonaConfig` 字段与 env `YUKI_PERSONA_*` 一致。
- **兼容**：`DEFAULT_PERSONA_PROMPT` 兜底保留；bridge 系统提示原样使用（不 `.format`）——既有 bridge 测试（默认 system_prompt 兜底）不受影响；e2e 不变（无偏好变化 → 跳过相同 → 不产生版本文件）。
- **测试注意**：`test_cli_diff_and_export_import` 中 store2 导入后 active 为 None（导入不设 active，符合实现）；cap 测试用 max_versions=2/3 验证清理与锁定豁免。
