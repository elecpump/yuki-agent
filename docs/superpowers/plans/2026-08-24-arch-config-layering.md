# 配置系统扩展 Implementation Plan（架构评审主题 8）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让配置支持插件自由扩展 + 用户本地覆盖，同时保留未知字段的快速失败保护。采用评审修正：**不放宽根模型 `extra="forbid"`**，而是新增 `plugins: dict[str, dict]` 未校验扩展容器 + `config.local.yaml` 用户覆盖层（deep_merge）。

**Architecture:** `Config` 根模型保持 `extra="forbid"`（拼写错误仍快速失败），新增 `plugins: dict[str, dict] = Field(default_factory=dict)` 作为插件配置容器（不校验内部结构）。`Config.load` 改为分层读取：系统 `config.yaml` → 用户 `config.local.yaml`（同目录，deep_merge 深度合并）→ 环境变量（最高优先级）。新增 `_deep_merge` 工具函数。

**Tech Stack:** Python ≥3.11，pydantic v2，pytest。无新增运行时依赖。

## Global Constraints

- 根模型 `model_config = ConfigDict(extra="forbid")` **保持不变**——未知顶级键仍抛 `ValidationError`（快速失败），不放宽为 warning。插件配置统一放进 `plugins:` 段落。
- 子模型（`BusConfig` 等）不新增 `plugins` 字段；只有根 `Config` 有 `plugins` 容器。
- 优先级（低→高）：默认值 → `config.yaml` → `config.local.yaml` → 环境变量。deep_merge 对 dict 深度合并、对非 dict 覆盖。
- 现有 `config.yaml` 单文件用法完全兼容：没有 `config.local.yaml` 时行为不变。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**修改**
- `src/yuki/config.py` — `plugins` 字段 + `_deep_merge` + `load` 分层
- `config.example.yaml` — 追加 `plugins` 示例段
- `tests/test_config.py`

---

### Task 1: plugins 扩展容器

**Files:**
- Modify: `src/yuki/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Config.plugins: dict[str, dict]`。Task 2 依赖（local 合并需保留 plugins 段）。

- [ ] **Step 1: 追加失败测试到 `tests/test_config.py`**

```python
def test_plugins_container_accepts_arbitrary_plugin_config():
    config = Config(plugins={"weather": {"api_key": "x", "units": "metric"}})
    assert config.plugins["weather"]["units"] == "metric"


def test_plugins_default_empty():
    assert Config().plugins == {}


def test_unknown_top_level_key_still_rejected():
    with pytest.raises(ValidationError):
        Config(typo_section={"x": 1})
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_config.py -v -k "plugins or unknown_top_level"`
Expected: FAIL（`plugins` 字段不存在 → `TypeError`/`ValidationError`）。

- [ ] **Step 3: 修改 `src/yuki/config.py`**

根 `Config` 增加字段：

```python
class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_name: str = "yuki"
    plugins: dict[str, dict] = Field(default_factory=dict)
    bus: BusConfig = Field(default_factory=BusConfig)
    ...
```

（保持 `extra="forbid"` 不变；`plugins` 是唯一不受结构校验的扩展容器。）

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 全 PASS（原 `extra="forbid"` 语义不变，`test_unknown_top_level_key_still_rejected` 通过）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/config.py tests/test_config.py
git commit -m "feat: add unvalidated plugins config container to root Config"
```

---

### Task 2: config.local.yaml 分层 + deep_merge

**Files:**
- Modify: `src/yuki/config.py`
- Modify: `config.example.yaml`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `Config.plugins`（Task 1）。
- Produces: `_deep_merge(base: dict, override: dict) -> dict`；`Config.load(config_file=None)` 分层合并：显式/自动 `config.yaml` → 同目录 `config.local.yaml` → 环境变量。

- [ ] **Step 1: 追加失败测试到 `tests/test_config.py`**

```python
def test_load_merges_local_override(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\nplugins:\n  weather:\n    units: metric\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        "bus:\n  base_port: 9000\nplugins:\n  weather:\n    units: imperial\n  maps:\n    zoom: 3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = Config.load(None)
    assert config.bus.base_port == 9000      # local 覆盖
    assert config.bus.hwm == 200             # local 未提 → 保留系统
    assert config.plugins["weather"]["units"] == "imperial"  # local 覆盖
    assert config.plugins["maps"]["zoom"] == 3               # local 新增
    assert config.plugins["weather"]["units"] == "imperial"


def test_load_env_overrides_local(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("bus:\n  base_port: 9000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_BASE_PORT", "7000")
    config = Config.load(None)
    assert config.bus.base_port == 7000


def test_load_without_local_file_unchanged(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = Config.load(None)
    assert config.bus.base_port == 8000


def test_load_explicit_file_merges_sibling_local(tmp_path):
    main = tmp_path / "main.yaml"
    local = tmp_path / "main.local.yaml"
    main.write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    local.write_text("bus:\n  hwm: 300\n", encoding="utf-8")
    config = Config.load(main)
    assert config.bus.base_port == 8000
    assert config.bus.hwm == 300
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_config.py -v -k "local or env_overrides_local or explicit_file"`
Expected: FAIL（当前 `load` 只读单个文件，不合并 local）。

- [ ] **Step 3: 修改 `src/yuki/config.py`**

- 新增 `_deep_merge`（模块级函数）：

```python
def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并：dict 递归合并，其余值覆盖。返回新 dict，不改入参。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
```

- 改写 `Config.load`：

```python
    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "Config":
        data: dict = {}
        path = Path(config_file) if config_file else None
        if path is None:
            default = Path("config.yaml")
            if default.exists():
                path = default
        if path is not None and path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data.update(yaml.safe_load(fh) or {})
        if path is not None:
            local = path.with_name(path.stem + ".local.yaml")
            if local.exists():
                with open(local, "r", encoding="utf-8") as fh:
                    data = _deep_merge(data, yaml.safe_load(fh) or {})
        cls._apply_env("persona_name", "PERSONA_NAME", data)
        for section_name, section_cls in (
            ("bus", BusConfig),
            ("logging", LoggingConfig),
            ("supervisor", SupervisorConfig),
            ("health", HealthConfig),
            ("memory", MemoryConfig),
            ("text", TextConfig),
            ("brain", BrainConfig),
            ("local_brain", LocalBrainConfig),
            ("vlm", VlmConfig),
            ("cloud", CloudConfig),
            ("soul", SoulConfig),
            ("perception", PerceptionConfig),
            ("wake_word", WakeWordConfig),
            ("gateway", GatewayConfig),
            ("context", ContextConfig),
            ("sedimenter", SedimenterConfig),
            ("persona", PersonaConfig),
        ):
            section = data.setdefault(section_name, {})
            for field_name in section_cls.model_fields:
                cls._apply_env(field_name, f"{section_name.upper()}_{field_name.upper()}", section, section_cls)
        return cls(**data)
```

注：`plugins` 段保持 dict 原样透传给根模型（不在 env 循环内）。

- [ ] **Step 4: 更新 `config.example.yaml`**

在文件末尾追加插件示例段（注释说明用途）：

```yaml
# 插件自由配置容器：任意键值，不经过 Pydantic 结构校验。
# 插件在 config.local.yaml 中可按同样结构覆盖/合并。
plugins:
  example_plugin:
    enabled: false
    options: {}
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/config.py config.example.yaml tests/test_config.py
git commit -m "feat: layer config.yaml over config.local.yaml with deep merge"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 8 全目标（采用评审修正版）——`plugins` 容器（Task 1）、`config.local.yaml` 分层 + deep_merge（Task 2）。**不**做"根模型放宽 `extra=forbid`"（评审明确否决策略，保留快速失败）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `_deep_merge(base, override)` 在 Step 3 定义、`load` 内调用一次；`plugins` 字段在 Task 1 定义、Task 2 测试透传验证；env 循环保持原 section 列表。
- **行为等价：** 无 local 文件时 `load` 行为与现在完全一致（Task 2 测试覆盖）；`extra="forbid"` 保留，拼写错误仍抛错。
