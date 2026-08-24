# WebSocket 通道注册 Implementation Plan（架构评审主题 6）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 gateway.py:478-553 三个近 30 行结构一致的 WS 闭包样板：新增 WS 通道只需定义 `WsChannelSpec` + 注册，不再复制 accept/register/touch/loop/unregister 模板。

**Architecture:** 新建 `src/yuki/bus_server/ws_channels.py`，定义 `WsChannelSpec`（route/channel_name/initial_message/queue_factory/message_handler/unregister_queue，全部以 runtime 为参数）与 `create_ws_handler(spec, connections, runtime)` 工厂 + 模块级注册表。gateway.py 把三个现成通道（status/chat/perception）声明为 spec 注册，`create_gateway_app` 循环 mount。`create_gateway_app(runtime, channels=None)` 支持注入 spec 列表，测试不污染全局注册表。

**Tech Stack:** Python ≥3.11，FastAPI/Starlette WebSocket，asyncio，pytest。无新增运行时依赖。

## Global Constraints

- **路由与消息形状完全不变**：`/ws/status`、`/ws/chat`、`/ws/perception` 的路由、初始消息、事件推送、interrupt 处理语义与现有 test_gateway.py 断言保持一致。
- `WsChannelSpec` 所有回调都以 `runtime` 为第一参数——通道在模块级注册，运行时才有实例，回调必须运行时可调用。
- `create_ws_handler(spec, connections, runtime)` 返回 `async def handler(websocket)`；handler 内 accept → register → send initial → 循环（push 或 request-response）→ finally unregister + unregister_queue。
- 注册表是模块级 `dict[str, WsChannelSpec]`，`register_ws_channel(spec) -> None` 幂等（同 route 覆盖）。`ws_channels() -> list[WsChannelSpec]` 返回副本。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**新增**
- `src/yuki/bus_server/ws_channels.py` — spec + 注册表 + handler 工厂（含从 gateway 迁入的 `_wait_for_ws_message_or_queue`）
- `tests/bus_server/test_ws_channels.py`

**修改**
- `src/yuki/bus_server/gateway.py` — 三通道改 spec 注册；`create_gateway_app` 循环 mount；删除内联闭包
- `tests/bus_server/test_gateway.py` — 追加"注入自定义通道"测试

---

### Task 1: WsChannelSpec + 注册表 + handler 工厂

**Files:**
- Create: `src/yuki/bus_server/ws_channels.py`
- Create: `tests/bus_server/test_ws_channels.py`

**Interfaces:**
- Consumes: 无（`GatewayRuntime` 仅作类型提示，用 `TYPE_CHECKING` 避免循环导入）。
- Produces: `WsChannelSpec`、`register_ws_channel(spec)`、`ws_channels() -> list[WsChannelSpec]`、`create_ws_handler(spec, connections, runtime)`、`_wait_for_ws_message_or_queue(websocket, queue)`。Task 2 依赖。

- [ ] **Step 1: 创建 `tests/bus_server/test_ws_channels.py`（先红）**

```python
import pytest

from yuki.bus_server.ws_channels import (
    WsChannelSpec,
    create_ws_handler,
    register_ws_channel,
    ws_channels,
)


def test_ws_channel_spec_is_frozen():
    with pytest.raises(Exception):
        WsChannelSpec(
            route="/ws/x",
            channel_name="x",
            initial_message=lambda runtime: {"type": "x"},
        ).route = "/ws/y"


def test_register_and_list_channels():
    register_ws_channel(WsChannelSpec(
        route="/ws/custom",
        channel_name="custom",
        initial_message=lambda runtime: {"type": "custom"},
    ))
    routes = {spec.route for spec in ws_channels()}
    assert "/ws/custom" in routes
    assert all(isinstance(spec, WsChannelSpec) for spec in ws_channels())


def test_create_ws_handler_returns_async_callable():
    spec = WsChannelSpec(
        route="/ws/x",
        channel_name="x",
        initial_message=lambda runtime: {"type": "x"},
    )
    handler = create_ws_handler(spec, connections=object(), runtime=object())
    assert callable(handler)
    import asyncio
    assert asyncio.iscoroutinefunction(handler)
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/bus_server/test_ws_channels.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.bus_server.ws_channels'`）。

- [ ] **Step 3: 创建 `src/yuki/bus_server/ws_channels.py`**

```python
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:  # 避免与 gateway.py 循环导入
    from starlette.websockets import WebSocket

    from yuki.bus_server.gateway import GatewayRuntime


@dataclass(frozen=True)
class WsChannelSpec:
    """一条 WS 通道的声明式描述；所有回调以 runtime 为第一参数。"""

    route: str
    channel_name: str
    initial_message: Callable[["GatewayRuntime"], dict] | None = None
    queue_factory: Callable[["GatewayRuntime"], asyncio.Queue | None] | None = None
    unregister_queue: Callable[["GatewayRuntime", asyncio.Queue], None] | None = None
    message_handler: Callable[["GatewayRuntime", dict], Awaitable[dict | None]] | None = None


_WS_CHANNELS: dict[str, WsChannelSpec] = {}


def register_ws_channel(spec: WsChannelSpec) -> None:
    _WS_CHANNELS[spec.route] = spec


def ws_channels() -> list[WsChannelSpec]:
    return list(_WS_CHANNELS.values())


def _wait_for_ws_message_or_queue(websocket, queue):
    """从 WS 消息或内部推送队列取一条：谁先到谁返回（None 表示队列空）。"""
    receive = asyncio.create_task(websocket.receive_text())
    queue_get = asyncio.create_task(queue.get()) if queue is not None else None
    try:
        done, pending = asyncio.wait(
            {t for t in (receive, queue_get) if t is not None},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if receive in done:
            return None  # 客户端发来文本时走 message_handler 或忽略
        if queue_get in done and queue_get is not None:
            return queue_get.result()
        return None
    except asyncio.CancelledError:
        for task in (receive, queue_get):
            if task is not None:
                task.cancel()
        raise


def create_ws_handler(spec: WsChannelSpec, connections, runtime):
    """生成统一的 WS handler：accept → register → initial → 循环 → cleanup。"""

    async def handler(websocket) -> None:
        await websocket.accept()
        connection_id = await connections.register(websocket, spec.channel_name)
        if spec.initial_message is not None:
            await websocket.send_json(spec.initial_message(runtime))
        updates = spec.queue_factory(runtime) if spec.queue_factory is not None else None
        try:
            while True:
                if spec.message_handler is not None:
                    message = await websocket.receive_json()
                    await connections.touch(connection_id)
                    reply = await spec.message_handler(runtime, message)
                    if reply is not None:
                        await websocket.send_json(reply)
                else:
                    message = await _wait_for_ws_message_or_queue(websocket, updates)
                    await connections.touch(connection_id)
                    if message is not None:
                        await websocket.send_json(message)
        except Exception as exc:
            from starlette.websockets import WebSocketDisconnect

            if not isinstance(exc, WebSocketDisconnect):
                raise
        finally:
            await connections.unregister(connection_id)
            if updates is not None and spec.unregister_queue is not None:
                spec.unregister_queue(runtime, updates)

    return handler
```

注：为精确匹配现有行为，push 通道（status/perception）用 `_wait_for_ws_message_or_queue`；chat 通道用 `message_handler`。`WebSocketDisconnect` 单独捕获返回，其余异常上抛。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/bus_server/test_ws_channels.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/bus_server/ws_channels.py tests/bus_server/test_ws_channels.py
git commit -m "feat: add declarative WsChannelSpec registry and handler factory"
```

---

### Task 2: gateway 迁移到通道注册

**Files:**
- Modify: `src/yuki/bus_server/gateway.py`
- Modify: `tests/bus_server/test_gateway.py`

**Interfaces:**
- Consumes: `WsChannelSpec`、`create_ws_handler`、`register_ws_channel`（Task 1）。
- Produces: `create_gateway_app(runtime, channels: list[WsChannelSpec] | None = None)`——channels 缺省取 `ws_channels()`；三个现成通道以 spec 注册。现有三通道路由/消息形状不变。

- [ ] **Step 1: 追加失败测试到 `tests/bus_server/test_gateway.py`**

```python
def test_gateway_mounts_injected_custom_channel():
    from yuki.bus_server.ws_channels import WsChannelSpec

    spec = WsChannelSpec(
        route="/ws/custom",
        channel_name="custom",
        initial_message=lambda runtime: {"type": "custom", "data": "hello"},
    )
    runtime, client = _client()
    runtime, client = GatewayRuntime(Config(), FakeBus()), None  # 重取干净实例
    from yuki.bus_server.gateway import create_gateway_app as _create

    runtime = GatewayRuntime(Config(), FakeBus())
    with TestClient(_create(runtime, channels=[spec])) as client:
        with client.websocket_connect("/ws/custom") as ws:
            assert ws.receive_json() == {"type": "custom", "data": "hello"}
```

（Step 1 实现时可化简为单一干净实例，避免重复构造；最终形态见 Step 3。）

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/bus_server/test_gateway.py::test_gateway_mounts_injected_custom_channel -v`
Expected: FAIL（`TypeError: create_gateway_app() got an unexpected keyword argument 'channels'`）。

- [ ] **Step 3: 修改 `src/yuki/bus_server/gateway.py`**

- import 区新增：

```python
from yuki.bus_server.ws_channels import WsChannelSpec, create_ws_handler, register_ws_channel, ws_channels
```

- 三个现成通道改为模块级 spec 注册（放在 `ConnectionManager` 之后、`GatewayRuntime` 之前，或用注册函数）：

```python
def _register_default_ws_channels() -> None:
    register_ws_channel(WsChannelSpec(
        route="/ws/status",
        channel_name="status",
        initial_message=lambda runtime: {"type": "health", "data": runtime.health_snapshot()},
        queue_factory=lambda runtime: runtime.register_status_queue(),
        unregister_queue=lambda runtime, q: runtime.unregister_status_queue(q),
    ))
    register_ws_channel(WsChannelSpec(
        route="/ws/perception",
        channel_name="perception",
        initial_message=lambda runtime: {"type": "snapshot", "data": runtime.perception_snapshot()},
        queue_factory=lambda runtime: runtime.register_perception_queue(),
        unregister_queue=lambda runtime, q: runtime.unregister_perception_queue(q),
    ))
    register_ws_channel(WsChannelSpec(
        route="/ws/chat",
        channel_name="chat",
        message_handler=_chat_message_handler,
    ))
```

`_register_default_ws_channels()` 在模块导入时调用一次（避免重复注册）。

chat 的 message_handler：

```python
async def _chat_message_handler(runtime, message: dict) -> dict | None:
    if message.get("type") == "interrupt":
        task = runtime.tasks.cancel_requested(str(message.get("task_id", "")))
        runtime.bus.publish("chat/interrupt", {"task_id": message.get("task_id")})
        return {"type": "interrupt_ack", "task": task}
    text = str(message.get("text") or message.get("user_input") or "")
    session_id = str(message.get("session_id") or "default")
    try:
        task = await asyncio.to_thread(runtime.run_chat, text, session_id)
    except Exception as exc:
        return {
            "type": "assistant_chunk",
            "task_id": "",
            "text": "",
            "done": True,
            "status": "failed",
            "error": str(exc),
        }
    result = task.get("result") or {}
    return {
        "type": "assistant_chunk",
        "task_id": task["task_id"],
        "text": result.get("text", ""),
        "done": True,
        "status": task["status"],
        "error": task.get("error", ""),
    }
```

- `create_gateway_app` 签名与 mount 循环：

```python
def create_gateway_app(runtime: GatewayRuntime, channels: list[WsChannelSpec] | None = None) -> FastAPI:
    connections = ConnectionManager(...)
    ...
    app = FastAPI(title="Yuki Gateway", lifespan=lifespan)
    ...
    for spec in (channels if channels is not None else ws_channels()):
        app.add_websocket_route(spec.route, create_ws_handler(spec, connections, runtime))
    return app
```

- **删除**原内联 `ws_status`/`ws_chat`/`ws_perception` 三个 `@app.websocket` 闭包，以及 gateway.py 内已迁入 ws_channels.py 的 `_wait_for_ws_message_or_queue` 定义（如存在）。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/bus_server/test_gateway.py tests/bus_server/test_ws_channels.py -v`
Expected: 全 PASS（三通道现有断言不变，新增注入通道测试通过）。

- [ ] **Step 5: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/bus_server/gateway.py src/yuki/bus_server/ws_channels.py tests/bus_server/test_gateway.py tests/bus_server/test_ws_channels.py
git commit -m "refactor: mount WebSocket channels from declarative registry"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 6 全目标——`WsChannelSpec` + 注册表（Task 1）、`create_ws_handler` 统一模板（Task 1）、gateway 循环 mount + 自定义通道注入（Task 2）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。Step 1 的测试草稿在 Step 3 明确"最终形态"简化说明，避免歧义。
- **Type consistency：** `WsChannelSpec` 字段名在 Task 1 定义、Task 2 注册同名使用；`create_ws_handler(spec, connections, runtime)` 在 Task 1 定义、Task 2 mount 同名调用；`_chat_message_handler(runtime, message)` 在 Task 2 定义、Task 2 注册引用。
- **行为等价：** push 通道（status/perception）走 `_wait_for_ws_message_or_queue`，chat 走 `message_handler`；`WebSocketDisconnect` 单独捕获返回；interrupt 语义原样保留。
