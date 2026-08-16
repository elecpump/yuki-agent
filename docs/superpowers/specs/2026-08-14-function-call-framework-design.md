# Yuki Function Call 框架设计

> 日期：2026-08-14
> 状态：已接入 CognitionAgent（2026-08-16 新增 `functions/call` 总线服务）
> 范围：Function Call 框架（注册表 + schema 导出 + 双入口分发 + 校验 + 进程间薄服务）；首批业务函数含 `system.ping` 与记忆工具

## 1. 背景与目标

为 Brain / L2 云桥提供 LLM 工具调用能力，同时让 L1 / CLI / 测试等程序化消费者复用同一套函数。本阶段交付：函数注册、JSON Schema 导出、两条调用入口（程序化 + LLM tool_call）、参数校验、结构化错误，以及跨进程 `functions/call` REQ/REP 服务。

**已确认决策**：
- **pydantic 模型派生 JSON Schema**：每个函数声明一个 pydantic 参数模型，`model_json_schema()` 导出工具清单；分发时 `model_validate(args)` 校验/强制转换。零新依赖（仓库已有 pydantic v2，与 Config 同风格）。
- **OpenAI 风格 tool_call**：LLM 面输入 `{"name", "arguments": "<JSON 字符串>"}`，未来 L2 云桥接 OpenAI 兼容 API 时直接兼容；程序化面仍用 dict。
- **范围外**：本阶段不做本地函数权限/审计模型，不把 TTS/感知能力开放为工具，不新增远端工具提供者协议。

## 2. 架构与文件布局

```
src/yuki/functions/
  __init__.py      — 导出 FunctionTool / FunctionRegistry / 异常
  registry.py      — FunctionTool dataclass + FunctionRegistry + 类型化异常
  service.py       — 将 registry.dispatch 暴露为 functions/call REQ/REP 服务
  system.py        — 内置示例函数 system.ping 的注册函数
tests/
  test_functions.py          — 注册/调用/schema 导出/异常
  functions/test_service.py  — functions/call 服务契约
```

- `registry.py`：机制（注册 + 校验 + 分发 + 导出），单一职责。
- `system.py`：首个示范注册函数，证明机制可用。
- `service.py`：只做服务名绑定和 `registry.dispatch` 转接，不持有业务状态。
- `CognitionAgent.setup()`：构建 registry、注册 `system.ping` 和记忆工具后，注册 `functions/call` 服务。

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

### 4.3 总线面 `functions/call`

认知层注册 REQ/REP 服务 `functions/call`，payload 与 `dispatch` 一致：

```json
{"name": "system.ping", "arguments": "{}"}
```

响应沿用 `dispatch` 结构：成功返回 `{ok: true, result}`，失败返回 `{ok: false, error}`。服务层不重新定义错误码，只把 registry 的结构化结果透传给调用方。

### 4.4 返回值契约

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
  - `functions/call`：经 FakeBus 请求服务，验证 dict 参数、OpenAI JSON 字符串参数和未知工具错误。
  - `register` 重名抛 `RegistryError`
  - `system.ping` 经注册表可用
- 认知层装配：`CognitionAgent.setup()` 同时注册 memory 服务和 `functions/call`，且可通过总线调用 `system.ping`。

## 8. 风险与兼容

- 兼容性：新增服务名，不改变既有主题、服务或 wire format。
- 运行时行为：`CognitionAgent` 新增一个 responder；服务只读调用已注册工具，不改变事件流。
- 新增依赖：无（复用 pydantic v2）。
- **后续接入点**（明确范围外）：本地函数权限/审计、TTS/感知工具开放、可观测面板、事件回放工具、远端工具提供者协议。

## 9. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 先做 registry，再以薄服务接入 cognition | registry 保持可单测；总线层只负责进程边界 |
| pydantic 模型派生 JSON Schema | 零新依赖；与 Config 同风格；天然得校验+coerce |
| OpenAI 风格 tool_call（arguments 为 JSON 串） | 未来 L2 云桥接 OpenAI 兼容 API 直接兼容 |
| `call` 抛异常 / `dispatch` 永不抛 | 程序化消费者要异常语义，LLM 消费者要可回填的结构化结果 |
| `functions/call` 透传 `dispatch` 结果 | 避免在服务层重复错误模型；所有入口共享同一语义 |
| handler 收到校验后的 pydantic 模型 | 类型安全；无参函数收 None |
