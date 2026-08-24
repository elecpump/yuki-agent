# 路由分发 registry Implementation Plan（架构评审主题 4）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `DecisionHub._handle_utterance` 的 if-elif 路由链（hub.py:261-267）替换为注册表分发：新增路由只需写一个 dispatcher + `register()`，不再改 hub 核心分发逻辑。

**Architecture:** 新建 `src/yuki/cognition/brain/route.py`，定义 `RouteDispatcher` Protocol（`can_handle`/`dispatch`）与 `DecisionRouter`（`register`/`dispatch` + fallback）。`hub.py` 把现有 `_dispatch_chat_local`/`_dispatch_tool_local`/`_dispatch_vision` 用一个小适配器包装注册，cloud 兜底作为 fallback dispatcher。crisis 检查与 local_enabled 门保持原位（安全路径不进入 registry）。

**Tech Stack:** Python ≥3.11（Protocol），pytest。无新增运行时依赖。

## Global Constraints

- **行为等价**：`_handle_utterance` 的对外行为不变——crisis 优先、local 未启用走 cloud、chat_local/tool_local/vision 分发、其余 cloud 兜底。现有 test_hub.py 全部保持通过。
- `RouteDispatcher.dispatch(text, snapshot, decision) -> dict` 返回 `_result` 形状（含 `rendered/spoke/reason/route/...`），与现有 dispatch 方法签名一致。
- 注册表是**纯结构**：不在 `DecisionHub.__init__` 之外维护全局可变状态；`DecisionRouter` 由 hub 持有。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/route.py` — `RouteDispatcher` + `DecisionRouter`
- `tests/cognition/test_route.py`

**修改**
- `src/yuki/cognition/brain/hub.py` — `_handle_utterance` 用 registry + 适配器
- `tests/cognition/test_hub.py` — 追加"注册新路由即可分发"测试

---

### Task 1: DecisionRouter 注册表 + 单测

**Files:**
- Create: `src/yuki/cognition/brain/route.py`
- Create: `tests/cognition/test_route.py`

**Interfaces:**
- Consumes: `RouterDecision`（`src/yuki/cognition/brain/local/router.py`）、`ContextSnapshot`（`src/yuki/cognition/context/snapshot.py`）。
- Produces: `RouteDispatcher` Protocol（`can_handle(decision) -> bool`、`dispatch(text, snapshot, decision) -> dict`）、`DecisionRouter`（`register(dispatcher)`、`dispatch(text, snapshot, decision, *, fallback) -> dict`）。Task 2 依赖。

- [ ] **Step 1: 创建 `tests/cognition/test_route.py`（先红）**

```python
import pytest

from yuki.cognition.brain.local.router import LocalRoute, RouterDecision
from yuki.cognition.brain.route import DecisionRouter
from yuki.cognition.context.snapshot import ContextSnapshot


class FakeDispatcher:
    def __init__(self, route, result):
        self._route = route
        self._result = result
        self.calls = []

    def can_handle(self, decision):
        return decision.route == self._route

    def dispatch(self, text, snapshot, decision):
        self.calls.append((text, decision))
        return self._result


class FallbackDispatcher:
    def can_handle(self, decision):
        return True

    def dispatch(self, text, snapshot, decision):
        return {"rendered": "fallback", "route": "fallback"}


def test_router_dispatches_to_first_matching():
    a = FakeDispatcher(LocalRoute.CHAT_LOCAL, {"rendered": "a"})
    b = FakeDispatcher(LocalRoute.VISION, {"rendered": "b"})
    router = DecisionRouter()
    router.register(a)
    router.register(b)
    decision = RouterDecision(LocalRoute.CHAT_LOCAL, 0.9)
    result = router.dispatch("hi", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["rendered"] == "a"
    assert a.calls == [("hi", decision)]


def test_router_falls_back_when_no_dispatcher_matches():
    router = DecisionRouter()
    decision = RouterDecision(LocalRoute.CLOUD, 0.1)
    result = router.dispatch("x", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["route"] == "fallback"


def test_router_returns_first_match_even_with_later_matches():
    d = FakeDispatcher(LocalRoute.CLOUD, {"rendered": "first"})
    e = FakeDispatcher(LocalRoute.CLOUD, {"rendered": "second"})
    router = DecisionRouter()
    router.register(d)
    router.register(e)
    decision = RouterDecision(LocalRoute.CLOUD, 0.8)
    result = router.dispatch("x", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["rendered"] == "first"
    assert len(d.calls) == 1
    assert len(e.calls) == 0
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_route.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.route'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/route.py`**

```python
from typing import Protocol

from yuki.cognition.brain.local.router import RouterDecision
from yuki.cognition.context.snapshot import ContextSnapshot


class RouteDispatcher(Protocol):
    """处理一类 RouterDecision 的分发器。can_handle 决定是否接管。"""

    def can_handle(self, decision: RouterDecision) -> bool: ...

    def dispatch(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
    ) -> dict: ...


class DecisionRouter:
    """按注册顺序分发 RouterDecision；首个 can_handle 匹配者接管，否则走 fallback。"""

    def __init__(self) -> None:
        self._dispatchers: list[RouteDispatcher] = []

    def register(self, dispatcher: RouteDispatcher) -> None:
        self._dispatchers.append(dispatcher)

    def dispatch(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
        *,
        fallback: RouteDispatcher,
    ) -> dict:
        for dispatcher in self._dispatchers:
            if dispatcher.can_handle(decision):
                return dispatcher.dispatch(text, snapshot, decision)
        return fallback.dispatch(text, snapshot, decision)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_route.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/route.py tests/cognition/test_route.py
git commit -m "feat: add RouteDispatcher protocol and DecisionRouter registry"
```

---

### Task 2: hub 接入 DecisionRouter

**Files:**
- Modify: `src/yuki/cognition/brain/hub.py`
- Modify: `tests/cognition/test_hub.py`

**Interfaces:**
- Consumes: `DecisionRouter`、`RouteDispatcher`（Task 1）。
- Produces: `DecisionHub.__init__` 内构建 `_route_registry`（私有属性）；`_handle_utterance` 经 registry 分发。新增 `register_route(dispatcher) -> None` 公开方法，供外部/测试注入自定义路由。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_hub.py`**

```python
def test_hub_can_register_custom_route(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))

    class CustomDispatcher:
        def can_handle(self, decision):
            return decision.route == "custom"

        def dispatch(self, text, snapshot, decision):
            return {
                "rendered": "custom-reply",
                "spoke": True,
                "reason": "custom",
                "route": "custom",
                "intent": Intent.UNKNOWN,
                "emotion": Emotion.NEUTRAL,
                "actions": [],
                "trusted_metadata": False,
            }

    decision = RouterDecision(LocalRoute.CLOUD, 0.9, reason="custom")
    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=FakeRouter(decision),
        local_composer=FakeComposer(),
        local_enabled=True,
    )
    hub.register_route(CustomDispatcher())

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "用自定义路由"})
    assert _reply_text(bus) == "custom-reply"
    memory.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_hub.py::test_hub_can_register_custom_route -v`
Expected: FAIL（`AttributeError: 'DecisionHub' object has no attribute 'register_route'`）。

- [ ] **Step 3: 修改 `src/yuki/cognition/brain/hub.py`**

- import 区新增：

```python
from yuki.cognition.brain.route import DecisionRouter, RouteDispatcher
```

- `DecisionHub.__init__` 末尾构建注册表：

```python
        self._route_registry = DecisionRouter()
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.CHAT_LOCAL, self._dispatch_chat_local,
        ))
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.TOOL_LOCAL, self._dispatch_tool_local,
        ))
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.VISION, self._dispatch_vision,
        ))
        self._route_fallback = _HubCloudFallback(self)
```

- 新增公开方法 `register_route` 与适配器类（放在 `DecisionHub` 类外、`LocalRoute` 导入后）：

```python
class _HubRouteDispatcher:
    """把 hub 现有 dispatch 方法包装为 RouteDispatcher。"""

    def __init__(self, route, handler) -> None:
        self._route = route
        self._handler = handler

    def can_handle(self, decision: RouterDecision) -> bool:
        return decision.route == self._route

    def dispatch(self, text, snapshot, decision) -> dict:
        return self._handler(text, snapshot, decision)


class _HubCloudFallback:
    """cloud 兜底：匹配一切，委托 hub._cloud_or_notice。"""

    def __init__(self, hub) -> None:
        self._hub = hub

    def can_handle(self, decision: RouterDecision) -> bool:
        return True

    def dispatch(self, text, snapshot, decision) -> dict:
        return self._hub._cloud_or_notice(
            text, snapshot, decision=decision, reason="cloud",
        )
```

- `_handle_utterance` 的路由链替换为：

```python
        decision = self._local_router.route(text, snapshot=snapshot, situation=situation)
        return self._route_registry.dispatch(
            text, snapshot, decision, fallback=self._route_fallback,
        )
```

- 新增方法：

```python
    def register_route(self, dispatcher: RouteDispatcher) -> None:
        """注册自定义路由分发器；can_handle 命中即接管对应 RouterDecision。"""
        self._route_registry.register(dispatcher)
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_hub.py tests/cognition/test_route.py -v`
Expected: 全 PASS（现有路由行为等价，新增自定义路由测试通过）。

- [ ] **Step 5: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/brain/hub.py tests/cognition/test_hub.py
git commit -m "refactor: dispatch hub routes via DecisionRouter registry"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 4 全覆盖——`RouteDispatcher` Protocol + `DecisionRouter`（Task 1），hub 接入 + 自定义路由可注册（Task 2）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `DecisionRouter.register/dispatch` 在 Task 1 定义、Task 2 同名调用；`RouteDispatcher` Protocol 签名在 Task 1 定义，`_HubRouteDispatcher`/`_HubCloudFallback` 实现一致；`register_route` 在 Step 1 测试与 Step 3 实现同名。
- **行为等价：** crisis 与 local_enabled 门在 `_handle_utterance` 原样保留（不进入 registry）；现有 test_hub.py 的 FakeRouter 路由行为不变。
