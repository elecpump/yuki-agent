# Yuki Function Call 框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Function Call 框架：函数注册 + JSON Schema 导出 + 双入口分发（程序化 `call` / LLM 面 `dispatch`）+ pydantic 参数校验，附带内置示例函数 `system.ping`。**先只做机制，不绑定首批业务函数。**

**Architecture:** 新增 `src/yuki/functions/` 包。`registry.py` 提供 `FunctionTool`（name/description/params 模型/handler）与 `FunctionRegistry`（register/tool 装饰器/names/call/dispatch/tool_schemas），参数用 pydantic 模型声明、`model_json_schema()` 导出、`model_validate()` 校验；`system.py` 注册内置 `system.ping`。纯新增库代码，不接入任何进程。

**Tech Stack:** Python ≥3.11，pydantic v2（已有依赖），stdlib `json`/`dataclasses`/`time`。无新增运行时依赖。

## Global Constraints

- 零新增运行时依赖：只用 stdlib + pydantic v2。
- 零协议变更、零运行时行为变化：纯新增模块，不触碰现有主题/服务/wire format，不接入进程。
- 函数参数 schema 用 pydantic 模型派生：`params: type[BaseModel] | None`（None=无参，handler 收 None）；导出用 `model_json_schema()`，校验用 `model_validate()`。
- LLM 面 tool_call 为 OpenAI 风格：`{"name": <str>, "arguments": "<JSON 字符串>"}`；`arguments` 缺失或空串视为 `{}`；`arguments` 也可直接是 dict（归一化）。
- `call(name, args) -> Any` 面向程序化消费者，抛异常（`ToolNotFoundError` / `ArgumentValidationError` / `ToolExecutionError`，基类 `FunctionError`）；`dispatch(tool_call) -> dict` 面向 LLM，**永不抛异常**，返回 `{"ok": bool, "result"|"error": ...}`；`error.code` ∈ `tool_not_found` / `invalid_arguments` / `handler_error`。
- 注册重名抛 `RegistryError`（继承 `FunctionError`）。
- `tool_schemas() -> list[dict]` 输出 OpenAI tools 格式；无参函数 `parameters` = `{"type": "object", "properties": {}}`。
- 内置 `system.ping`：无参，返回 `{"ok": True, "ts": <time.time()>}`；`register_builtin_system(registry)` 注册之。
- handler 返回值必须 JSON 可序列化。
- 并发：注册在装配阶段完成，之后只读调用，无需锁（文档注明"先注册后使用"）。
- 测试命令（仓库根目录）：`& ".venv\Scripts\python.exe" -m pytest <文件> -v`。全仓回归 `python -m pytest`（e2e 默认跳过）。
- 设计文档：`docs/superpowers/specs/2026-08-14-function-call-framework-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/functions/__init__.py` — 导出 `FunctionTool`/`FunctionRegistry`/四异常/`register_builtin_system`
- `src/yuki/functions/registry.py` — 异常 + `FunctionTool` + `FunctionRegistry`
- `src/yuki/functions/system.py` — 内置 `system.ping`
- `tests/test_functions.py` — 注册/调用/schema/异常（程序化面）
- `tests/test_dispatch.py` — LLM 面分发

---

### Task 1: 注册表核心（异常 + FunctionTool + register/tool/names/call）

**Files:**
- Create: `src/yuki/functions/registry.py`
- Test: `tests/test_functions.py`

**Interfaces:**
- Consumes: 无。
- Produces: `FunctionError(Exception)`；`RegistryError`/`ToolNotFoundError`/`ArgumentValidationError`/`ToolExecutionError` 均继承之；`FunctionTool(name, description, params: type[BaseModel] | None, handler: Callable[[BaseModel | None], Any])` frozen dataclass；`FunctionRegistry()` 方法 `register(tool)`（重名抛 `RegistryError`）、`tool(name, *, description, params=None)` 装饰器、`names() -> list[str]`（排序）、`call(name, args: dict | None = None) -> Any`。内部 `_validate(tool, args) -> BaseModel | None`（Task 2 复用）。Task 2 添加 `dispatch`/`tool_schemas`。

- [ ] **Step 1: 写失败测试 `tests/test_functions.py`**

```python
import pytest
from pydantic import BaseModel, Field

from yuki.functions.registry import (
    ArgumentValidationError,
    FunctionRegistry,
    RegistryError,
    ToolExecutionError,
    ToolNotFoundError,
)


class EchoParams(BaseModel):
    text: str = Field(description="要回显的文本")
    times: int = 1


@pytest.fixture()
def registry():
    r = FunctionRegistry()

    def echo(params: EchoParams) -> str:
        return params.text * params.times

    r.tool("echo", description="回显文本 times 次", params=EchoParams)(echo)
    return r


def test_register_and_call(registry):
    assert registry.call("echo", {"text": "ab", "times": 3}) == "ababab"


def test_call_coerces_types(registry):
    assert registry.call("echo", {"text": "x", "times": "3"}) == "xxx"


def test_call_missing_optional_arg_uses_default(registry):
    assert registry.call("echo", {"text": "y"}) == "y"


def test_call_unknown_tool_raises(registry):
    with pytest.raises(ToolNotFoundError):
        registry.call("nope", {})


def test_call_invalid_args_raises(registry):
    with pytest.raises(ArgumentValidationError):
        registry.call("echo", {"text": 123})


def test_call_handler_error_wraps(registry):
    def boom(params):
        raise RuntimeError("kaboom")

    registry.tool("boom", description="总是失败", params=None)(boom)
    with pytest.raises(ToolExecutionError, match="kaboom"):
        registry.call("boom")


def test_register_duplicate_raises(registry):
    with pytest.raises(RegistryError):
        registry.tool("echo", description="dup", params=EchoParams)(lambda p: "")


def test_names_sorted(registry):
    registry.tool("zeta", description="z", params=None)(lambda p: 1)
    assert registry.names() == ["echo", "zeta"]


def test_no_params_tool_ignores_args(registry):
    registry.tool("noop", description="无参", params=None)(lambda p: p)
    assert registry.call("noop", {"junk": 1}) is None
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_functions.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.functions'`）。

- [ ] **Step 3: 创建 `src/yuki/functions/registry.py`**

```python
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


class FunctionError(Exception):
    """函数调用框架基类异常。"""


class RegistryError(FunctionError):
    """注册冲突或非法注册。"""


class ToolNotFoundError(FunctionError):
    """调用了未注册的工具。"""


class ArgumentValidationError(FunctionError):
    """参数解析或校验失败。"""


class ToolExecutionError(FunctionError):
    """handler 执行时抛出的异常。"""


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    params: type[BaseModel] | None
    handler: Callable[[BaseModel | None], Any]


class FunctionRegistry:
    """函数注册表：注册 → 校验 → 分发/调用 → schema 导出。

    注册应在装配阶段完成，之后只读调用；call/dispatch 每次调用无共享状态，无需加锁。
    handler 的返回值必须 JSON 可序列化。
    """

    def __init__(self) -> None:
        self._tools: dict[str, FunctionTool] = {}

    def register(self, tool: FunctionTool) -> None:
        if tool.name in self._tools:
            raise RegistryError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        *,
        description: str,
        params: type[BaseModel] | None = None,
    ):
        def decorator(fn: Callable[[BaseModel | None], Any]) -> Callable[[BaseModel | None], Any]:
            self.register(FunctionTool(name=name, description=description, params=params, handler=fn))
            return fn

        return decorator

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: dict | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        validated = self._validate(tool, args)
        try:
            return tool.handler(validated)
        except FunctionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"{name} failed: {exc}") from exc

    def _validate(self, tool: FunctionTool, args: dict | None) -> BaseModel | None:
        if tool.params is None:
            return None
        try:
            return tool.params.model_validate(args or {})
        except ValidationError as exc:
            raise ArgumentValidationError(f"{tool.name}: {exc}") from exc
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_functions.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/functions/registry.py tests/test_functions.py
git commit -m "feat: add function registry with typed tool registration and call"
```

---

### Task 2: LLM 面 dispatch + tool_schemas 导出

**Files:**
- Modify: `src/yuki/functions/registry.py`（追加 `dispatch` / `tool_schemas` / `_parse_arguments`）
- Test: `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `FunctionRegistry`/`FunctionTool`/`ArgumentValidationError`（Task 1）。
- Produces: `registry.dispatch(tool_call: dict) -> dict`（永不抛；`{"ok": True, "result": ...}` 或 `{"ok": False, "error": {"code", "message"}}`）；`registry.tool_schemas() -> list[dict]`（OpenAI tools 格式）。Task 3 不依赖本任务。

- [ ] **Step 1: 写失败测试 `tests/test_dispatch.py`**

```python
import json

from pydantic import BaseModel, Field

from yuki.functions.registry import FunctionRegistry


class EchoParams(BaseModel):
    text: str = Field(description="要回显的文本")
    times: int = 1


def _boom(params):
    raise RuntimeError("handler boom")


def make_registry() -> FunctionRegistry:
    r = FunctionRegistry()
    r.tool("echo", description="回显", params=EchoParams)(lambda p: p.text * p.times)
    r.tool("boom", description="失败", params=None)(_boom)
    r.tool("noop", description="无参", params=None)(lambda p: {"done": True})
    return r


def test_dispatch_ok():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": json.dumps({"text": "ab", "times": 2})})
    assert result == {"ok": True, "result": "abab"}


def test_dispatch_arguments_as_dict_ok():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": {"text": "x", "times": 3}})
    assert result["ok"] is True
    assert result["result"] == "xxx"


def test_dispatch_arguments_missing_or_empty_ok():
    r = make_registry()
    assert r.dispatch({"name": "noop"}) == {"ok": True, "result": {"done": True}}
    assert r.dispatch({"name": "noop", "arguments": ""}) == {"ok": True, "result": {"done": True}}
    assert r.dispatch({"name": "noop", "arguments": "   "}) == {"ok": True, "result": {"done": True}}


def test_dispatch_unknown_tool():
    r = make_registry()
    result = r.dispatch({"name": "missing", "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_found"


def test_dispatch_name_not_string():
    r = make_registry()
    result = r.dispatch({"name": {"not": "a string"}, "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_found"


def test_dispatch_invalid_json():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": "{oops"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_non_object_json():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": "[1,2]"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_schema_violation():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": json.dumps({"text": 123})})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_handler_error():
    r = make_registry()
    result = r.dispatch({"name": "boom", "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "handler_error"
    assert "handler boom" in result["error"]["message"]


def test_tool_schemas_shape():
    r = make_registry()
    schemas = r.tool_schemas()
    by_name = {s["function"]["name"]: s for s in schemas}
    assert set(by_name) == {"echo", "boom", "noop"}
    echo = by_name["echo"]["function"]
    assert by_name["echo"]["type"] == "function"
    assert echo["description"] == "回显"
    assert echo["parameters"]["type"] == "object"
    assert "text" in echo["parameters"]["properties"]
    assert echo["parameters"]["required"] == ["text"]
    assert by_name["noop"]["function"]["parameters"] == {"type": "object", "properties": {}}
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_dispatch.py -v`
Expected: FAIL（`AttributeError: 'FunctionRegistry' object has no attribute 'dispatch'`）。

- [ ] **Step 3: 在 `src/yuki/functions/registry.py` 追加方法**

在 `FunctionRegistry` 类中、`call` 方法之后追加：

```python
    def dispatch(self, tool_call: dict) -> dict:
        name = tool_call.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return {"ok": False, "error": {"code": "tool_not_found", "message": f"unknown tool: {name!r}"}}
        try:
            args = self._parse_arguments(tool_call.get("arguments"))
        except ArgumentValidationError as exc:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        try:
            validated = self._validate(tool, args)
        except ArgumentValidationError as exc:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        try:
            result = tool.handler(validated)
        except Exception as exc:
            return {"ok": False, "error": {"code": "handler_error", "message": str(exc)}}
        return {"ok": True, "result": result}

    def tool_schemas(self) -> list[dict]:
        schemas = []
        for name in self.names():
            tool = self._tools[name]
            if tool.params is None:
                parameters: dict = {"type": "object", "properties": {}}
            else:
                parameters = tool.params.model_json_schema()
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": parameters,
                },
            })
        return schemas

    def _parse_arguments(self, raw) -> dict:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ArgumentValidationError(f"arguments not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ArgumentValidationError("arguments must be a JSON object")
            return parsed
        raise ArgumentValidationError("arguments must be a string or object")
```

并在文件顶部导入追加 `import json`：

```python
import json
from dataclasses import dataclass
from typing import Any, Callable
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_dispatch.py tests/test_functions.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/functions/registry.py tests/test_dispatch.py
git commit -m "feat: add LLM-facing dispatch and OpenAI tool schema export"
```

---

### Task 3: 内置 system.ping + 包导出

**Files:**
- Create: `src/yuki/functions/system.py`、`src/yuki/functions/__init__.py`
- Test: `tests/test_functions.py`（追加）

**Interfaces:**
- Consumes: `FunctionRegistry`（Task 1）。
- Produces: `register_builtin_system(registry)` 注册 `system.ping`（无参，返回 `{"ok": True, "ts": <float>}`）；`src/yuki/functions/__init__.py` 导出 `FunctionError`/`RegistryError`/`ToolNotFoundError`/`ArgumentValidationError`/`ToolExecutionError`/`FunctionTool`/`FunctionRegistry`/`register_builtin_system`。

- [ ] **Step 1: 追加失败测试到 `tests/test_functions.py`**

```python
from yuki.functions import FunctionError, FunctionRegistry, register_builtin_system


def test_builtin_system_ping():
    r = FunctionRegistry()
    register_builtin_system(r)
    assert "system.ping" in r.names()
    result = r.call("system.ping")
    assert result["ok"] is True
    assert isinstance(result["ts"], float)


def test_builtin_system_ping_dispatchable():
    r = FunctionRegistry()
    register_builtin_system(r)
    schemas = r.tool_schemas()
    assert any(s["function"]["name"] == "system.ping" for s in schemas)
    out = r.dispatch({"name": "system.ping", "arguments": "{}"})
    assert out["ok"] is True
    assert isinstance(out["result"]["ts"], float)


def test_package_exports_errors():
    assert issubclass(FunctionError, Exception)
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_functions.py -v`
Expected: FAIL（`ImportError: cannot import name 'register_builtin_system'`）。

- [ ] **Step 3: 创建 `src/yuki/functions/system.py`**

```python
import time

from yuki.functions.registry import FunctionRegistry


def register_builtin_system(registry: FunctionRegistry) -> None:
    """注册内置系统函数（当前仅 system.ping，作健康/演示）。"""

    @registry.tool("system.ping", description="健康/演示：无参数心跳，返回当前时间戳。")
    def _ping(params=None) -> dict:
        return {"ok": True, "ts": time.time()}
```

- [ ] **Step 4: 创建 `src/yuki/functions/__init__.py`**

```python
from yuki.functions.registry import (
    ArgumentValidationError,
    FunctionError,
    FunctionRegistry,
    FunctionTool,
    RegistryError,
    ToolExecutionError,
    ToolNotFoundError,
)
from yuki.functions.system import register_builtin_system

__all__ = [
    "FunctionError",
    "RegistryError",
    "ToolNotFoundError",
    "ArgumentValidationError",
    "ToolExecutionError",
    "FunctionTool",
    "FunctionRegistry",
    "register_builtin_system",
]
```

- [ ] **Step 5: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_functions.py tests/test_dispatch.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（e2e 默认跳过；此前 218 passed。注意：若主仓 venv 缺少 perception/interaction 运行时依赖，需先补齐——见 Global Constraints 上方说明）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/functions/system.py src/yuki/functions/__init__.py tests/test_functions.py
git commit -m "feat: add builtin system.ping and package exports"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1/2/3；§3 FunctionTool + 两种注册方式 → Task 1；§4.1 `call` 三异常 → Task 1；§4.2 `dispatch` 永不抛 + 四类 code → Task 2；§5 `tool_schemas` + 无参 parameters 形状 + `system.ping` + `names()` → Task 2/3；§6 无锁/先注册后使用 → registry.py docstring；§7 测试清单 → 各任务；§8 零依赖/零协议变更 → Global Constraints。
- **一致性**：`_validate(tool, args)` 在 Task 1 定义、Task 2 复用；`dispatch` 与 `call` 共用同一校验路径；`error.code` 枚举与测试断言一致；`FunctionError` 异常层级在 Task 1 定义、`__init__.py` 全量导出。
