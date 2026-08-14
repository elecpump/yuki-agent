# Yuki Function Call 框架设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：Function Call 框架（注册表 + schema 导出 + 双入口分发 + 校验）；**先只做机制，不绑定首批业务函数**

## 1. 背景与目标

为未来 Brain / L2 云桥提供 LLM 工具调用能力，同时让 L1 / CLI / 测试等程序化消费者复用同一套函数。本次**只交付框架机制**：函数注册、JSON Schema 导出、两条调用入口（程序化 + LLM tool_call）、参数校验、结构化错误。

**已确认决策**：
- **pydantic 模型派生 JSON Schema**：每个函数声明一个 pydantic 参数模型，`model_json_schema()` 导出工具清单；分发时 `model_validate(args)` 校验/强制转换。零新依赖（仓库已有 pydantic v2，与 Config 同风格）。
- **OpenAI 风格 tool_call**：LLM 面输入 `{"name", "arguments": "<JSON 字符串>"}`，未来 L2 云桥接 OpenAI 兼容 API 时直接兼容；程序化面仍用 dict。
- **范围外**：首批不绑定记忆/TTS/感知等业务函数；不接入 CognitionAgent；不做跨进程 `functions/call` 总线服务（Brain 阶段再补薄层）。

## 2. 架构与文件布局

```
src/yuki/functions/
  __init__.py      — 导出 FunctionTool / FunctionRegistry / 异常
  registry.py      — FunctionTool dataclass + FunctionRegistry + 类型化异常
  system.py        — 内置示例函数 system.ping 的注册函数
tests/
  test_functions.py — 注册/调用/schema 导出/异常
  test_dispatch.py  — OpenAI 风格 tool_call 分发/校验/结构化错误
```

- `registry.py`：机制（注册 + 校验 + 分发 + 导出），单一职责。
- `system.py`：首个示范注册函数，证明机制可用。
- 不接入 `CognitionAgent`（Brain 尚不存在，避免 YAGNI 死代码）。

## 3. FunctionTool 与注册

```python
@dataclass
class FunctionTool:
    name: str
    description: str
    params: type[BaseModel] | None          # pydantic 参数模型；None=无参数
    handler: Callable[[BaseModel | None], Any]  # 收到校验后的模型实例（或 None）
```

`FunctionRegistry` 提供两种注册方式：
- 显式：`register(tool: FunctionTool)`，重名抛 `RegistryError`。
- 装饰器：`@registry.tool("memory.query", description="...", params=QueryParams)` 包裹 `def handler(params) -> Any`。

函数本体（handler + 参数模型）只注册一次，两个调用入口共用同一套校验。

## 4. 两条调用入口 + 错误模型

### 4.1 程序化面 `call(name: str, args: dict | None = None) -> Any`

面向 L1 / CLI / 测试等代码消费者，校验失败**抛异常**：

| 情况 | 异常 |
|---|---|
| 未知工具 | `ToolNotFoundError` |
| 参数校验失败 | `ArgumentValidationError`（含 pydantic 校验详情） |
| handler 抛异常 | `ToolExecutionError`（包装原始异常，message 含 `str(orig)`） |

异常层级：`FunctionError(Exception)` 为基类，四者继承。

### 4.2 LLM 面 `dispatch(tool_call: dict) -> dict`

面向未来 Brain / L2，输入 OpenAI 风格 `{"name": ..., "arguments": "<JSON 字符串>"}`。**永不抛异常**，始终返回结构化 dict：

- 成功：`{"ok": True, "result": <JSON 可序列化值>}`
- 失败：`{"ok": False, "error": {"code": "tool_not_found" | "invalid_arguments" | "handler_error", "message": "..."}}`

`arguments` 为 JSON 字符串，解析失败归入 `invalid_arguments`；`arguments` **缺失或为空串**均视为 `{}`。两条入口都先 `model_validate(args)` 再调 handler。

### 4.3 返回值契约

handler 必须返回 JSON 可序列化的值（未来经总线/云桥直接可用）。

## 5. Schema 导出 + 内置示例

```python
def tool_schemas(self) -> list[dict]:
    # [{"type": "function",
    #   "function": {"name", "description",
    #                "parameters": <params.model_json_schema()>}}]
```

- 无参函数 `parameters` = `{"type": "object", "properties": {}}`。
- 内置 `system.ping`：无参，返回 `{"ok": True, "ts": <time.time()>}`，作健康/演示函数。
- `names() -> list[str]` 返回已注册函数名。

## 6. 并发与生命周期

- 注册发生在装配（setup）阶段，之后只读调用，`call`/`dispatch` 每次调用无共享状态 → **无需锁**；文档注明"先注册后使用"。
- `FunctionRegistry()` 构造为空注册表；内置函数经 `system.py` 的 `register_builtin_system(registry)` 注册。

## 7. 测试

- 测试用自定义 tool（带参数模型）覆盖：
  - `call` 正常路径 + pydantic 强制转换（如 str→int）
  - `call` 三类异常（`ToolNotFoundError` / `ArgumentValidationError` / `ToolExecutionError`）
  - `tool_schemas` 输出形状（OpenAI tools 格式、无参函数 parameters 形状）
  - `dispatch`：arguments JSON 字符串解析、未知工具、非法参数、handler 异常 → 结构化 `{ok: False}`；正常 → `{ok: True}`
  - `register` 重名抛 `RegistryError`
  - `system.ping` 经注册表可用
- e2e 行为等价：现有断言不变（本模块纯新增，不改变数据流）。

## 8. 风险与兼容

- 零协议变更：纯新增模块，不触碰现有主题/服务/wire format。
- 零运行时行为变化：不接入任何进程，仅新增库代码。
- 新增依赖：无（复用 pydantic v2）。
- **后续接入点**（明确范围外）：Brain 阶段在 cognition 内 `registry = FunctionRegistry()` + `register_builtin_system`，注册记忆/回复等函数，把 `dispatch` 接到 LLM tool_calls；跨进程调用可加 `functions/call` REQ/REP 薄层。

## 9. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 先只做框架，不绑首批业务函数 | YAGNI；机制就绪后绑定是一行注册的事 |
| pydantic 模型派生 JSON Schema | 零新依赖；与 Config 同风格；天然得校验+coerce |
| OpenAI 风格 tool_call（arguments 为 JSON 串） | 未来 L2 云桥接 OpenAI 兼容 API 直接兼容 |
| `call` 抛异常 / `dispatch` 永不抛 | 程序化消费者要异常语义，LLM 消费者要可回填的结构化结果 |
| 不接入 CognitionAgent / 不做总线服务 | Brain 尚不存在；避免死代码与过早抽象 |
| handler 收到校验后的 pydantic 模型 | 类型安全；无参函数收 None |
