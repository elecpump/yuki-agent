# Yuki L2 云桥 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 L2 云端深度回应通道：OpenAI 兼容 CloudClient、工具调用多轮 CloudBridge、L1/L2 层级选择、降级回退、记忆函数绑定、接入 DecisionHub。

**Architecture:** `src/yuki/cognition/l2/` 包（client/context/bridge 三层）+ `src/yuki/functions/memory_tools.py`（记忆函数绑定）。`DecisionPolicy` 新增 `tier_for(intent)`（不改 `decide` 签名）；`DecisionHub` 新增 `bridge`，UTTERANCE 按 tier 路由：L2→bridge.generate（CloudError 回退 L1），L1→现有动作链。`cloud:` 配置默认关，保持现有行为。

**Tech Stack:** Python ≥3.11，stdlib `urllib.request` + 现有 pydantic。零新增运行时依赖。

## Global Constraints

- 零新增运行时依赖（`urllib.request`）；零协议变更（REPLY 主题/载荷不变）。
- OpenAI 兼容端点：`POST {base_url}/chat/completions`，`Authorization: Bearer <api_key>`（有 key 时）。
- `CloudClient.chat` 任何失败（网络/超时/HTTP 非 2xx/解析/缺 choices）→ `CloudError`。
- `CloudBridge.generate`：system(persona) + user(context+utterance)；有 registry 时带 `tools=tool_schemas()`；tool_calls 逐个 `registry.dispatch` 回填（`role:"tool"`）；循环至 `max_turns`；空最终文本 → `CloudError`；任何异常 → `CloudError`。
- `build_cloud_context`：**纯文本**（utterance + 情境 topic/summary/key_points + 记忆 top-k=3），记忆**过滤 `sensitivity == 2`**。
- 记忆工具隐私硬约束：`memory.query/list/get` 结果统一过滤 `sensitivity == 2`（云端不可取高敏记忆）。
- `DecisionPolicy.tier_for(intent) -> Tier`：`L2_INTENTS = {ENTERTAINMENT, CREATIVE, ROLEPLAY, GAME, EMOTIONAL}` → l2，其余（含 SAFETY）→ l1。`decide` 签名不变。
- `DecisionHub(bus, ..., bridge=None)`：tier=L2 且 bridge 非 None → `bridge.generate`（同步阻塞）→ REPLY；CloudError → 回退 `policy.decide` L1 动作链；tier=L2 但 bridge None → L1。awake/situation 恒 L1。`DecisionTrace` 增 `tier` 字段。
- `cloud:` 配置默认 `enabled: false`；api_key 仅环境变量（`api_key_env`，默认 `YUKI_CLOUD_API_KEY`）。
- `CognitionAgent.setup`：registry 构建后 `register_memory_functions`；`cloud.enabled` 时构建 CloudBridge 传给 `build_brain`；health 增 `l2`（未启用=正常）。
- e2e 行为等价：cloud 默认关，`[yuki] 我在,你说。` 与 REPLY 主题不变。
- 测试命令（仓库根）：`& ".venv\Scripts\python.exe" -m pytest <文件> -v`；全仓 `-m pytest`。
- 设计文档：`docs/superpowers/specs/2026-08-14-l2-cloud-bridge-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/cognition/l2/__init__.py`、`client.py`、`context.py`、`bridge.py`
- `src/yuki/functions/memory_tools.py`
- `tests/cognition/l2/test_client.py`、`tests/cognition/l2/test_context.py`、`tests/cognition/l2/test_bridge.py`
- `tests/functions/test_memory_tools.py`

**修改**
- `src/yuki/config.py`、`config.example.yaml`（cloud 节）、`tests/test_config.py`
- `src/yuki/cognition/brain/policy.py`（Tier + tier_for + L2_INTENTS）、`tests/cognition/test_policy.py`
- `src/yuki/cognition/brain/hub.py`（bridge + tier 路由 + trace tier）、`tests/cognition/test_hub.py`
- `src/yuki/cognition/agent.py`（绑记忆函数 + 桥构建 + health l2）、`tests/cognition/test_cognition.py`

---

### Task 1: CloudClient + cloud 配置

**Files:**
- Create: `src/yuki/cognition/l2/client.py`、`src/yuki/cognition/l2/__init__.py`
- Modify: `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- Test: `tests/cognition/l2/test_client.py`

**Interfaces:**
- Consumes: 无。
- Produces: `CloudError(Exception)`；`CloudClient(base_url, model, api_key=None, timeout_s=10.0, post=None)` 方法 `chat(messages: list[dict], tools: list[dict] | None = None) -> dict`（`post` 可注入，默认 `_default_post` 用 urllib）。`Config.cloud`（`CloudConfig`: enabled=False、base_url、model、api_key_env、timeout_s、max_turns）。Task 3 依赖。

- [ ] **Step 1: 追加 cloud 配置测试到 `tests/test_config.py`**

```python
def test_cloud_defaults():
    config = Config()
    assert config.cloud.enabled is False
    assert config.cloud.base_url == "https://api.openai.com/v1"
    assert config.cloud.model == "gpt-4o-mini"
    assert config.cloud.api_key_env == "YUKI_CLOUD_API_KEY"
    assert config.cloud.timeout_s == 10.0
    assert config.cloud.max_turns == 3


def test_cloud_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YUKI_CLOUD_MODEL", "gpt-5")
    monkeypatch.setenv("YUKI_CLOUD_TIMEOUT_S", "20.0")
    config = Config.load(None)
    assert config.cloud.enabled is True
    assert config.cloud.model == "gpt-5"
    assert config.cloud.timeout_s == 20.0
```

- [ ] **Step 2: 写失败测试 `tests/cognition/l2/test_client.py`**

```python
import pytest

from yuki.cognition.l2.client import CloudClient, CloudError


def test_chat_posts_correct_request():
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": "hi"}}]}

    client = CloudClient("https://api.example.com/v1", "m1", api_key="k", timeout_s=5.0, post=fake_post)
    result = client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert result["choices"][0]["message"]["content"] == "hi"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["payload"]["model"] == "m1"
    assert captured["payload"]["tools"] == [{"type": "function"}]
    assert captured["timeout"] == 5.0


def test_chat_without_api_key_omits_auth():
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["headers"] = headers
        return {"choices": [{"message": {"content": "x"}}]}

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    client.chat([{"role": "user", "content": "x"}])
    assert "Authorization" not in captured["headers"]


def test_chat_propagates_cloud_error():
    def fake_post(url, headers, payload, timeout):
        raise CloudError("HTTP 429")

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError, match="429"):
        client.chat([])


def test_chat_maps_network_error_to_cloud_error():
    def fake_post(url, headers, payload, timeout):
        raise TimeoutError("timed out")

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError):
        client.chat([])


def test_chat_rejects_missing_choices():
    def fake_post(url, headers, payload, timeout):
        return {}

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError, match="choices"):
        client.chat([])
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_client.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.l2'`）。

- [ ] **Step 4: 创建 `src/yuki/cognition/l2/client.py`**

```python
import json
import urllib.error
import urllib.request
from typing import Callable


class CloudError(Exception):
    """云端调用失败（网络/超时/HTTP/解析/空响应）。"""


def _default_post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CloudError(f"HTTP {exc.code}") from exc
    except json.JSONDecodeError as exc:
        raise CloudError(f"invalid JSON response: {exc}") from exc


class CloudClient:
    """OpenAI 兼容 chat/completions 客户端。post 可注入以便测试。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        post: Callable[[str, dict, dict, float], dict] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self._post = post or _default_post

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            raw = self._post(f"{self._base}/chat/completions", headers, payload, self._timeout)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"network error: {exc}") from exc
        if not isinstance(raw, dict) or not raw.get("choices"):
            raise CloudError("invalid response: no choices")
        return raw
```

- [ ] **Step 5: 创建 `src/yuki/cognition/l2/__init__.py`（Task 1 阶段先空文件，Task 6 补导出）**

```python
# 空文件。Task 6 完成后补导出。
```

- [ ] **Step 6: `src/yuki/config.py` 加 CloudConfig 并注册**

在 `BrainConfig` 之后新增：

```python
class CloudConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "YUKI_CLOUD_API_KEY"
    timeout_s: float = Field(10.0, ge=0.1)
    max_turns: int = Field(3, ge=1)
```

在 `Config` 中 `brain` 字段之后新增：

```python
    cloud: CloudConfig = Field(default_factory=CloudConfig)
```

在 `Config.load` 的 section 元组中 `("brain", BrainConfig),` 之后新增：

```python
            ("cloud", CloudConfig),
```

- [ ] **Step 7: `config.example.yaml` 加 cloud 节**

```yaml
cloud:
  enabled: false
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: YUKI_CLOUD_API_KEY
  timeout_s: 10.0
  max_turns: 3
```

- [ ] **Step 8: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_client.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 9: Commit**

```bash
git add src/yuki/cognition/l2/client.py src/yuki/cognition/l2/__init__.py src/yuki/config.py config.example.yaml tests/cognition/l2/test_client.py tests/test_config.py
git commit -m "feat: add OpenAI-compatible cloud client and cloud config"
```

---

### Task 2: 云端上下文（context.py）

**Files:**
- Create: `src/yuki/cognition/l2/context.py`
- Test: `tests/cognition/l2/test_context.py`

**Interfaces:**
- Consumes: `MemoryManager`（query/list）。
- Produces: `build_cloud_context(utterance: str, situation: dict | None, memory: MemoryManager | None) -> str`。Task 3 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/l2/test_context.py`**

```python
from yuki.cognition.l2.context import build_cloud_context
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_context_includes_utterance_and_situation():
    ctx = build_cloud_context("你好", {"topic": "量子计算", "summary": "介绍", "key_points": ["a", "b"]})
    assert "你好" in ctx
    assert "量子计算" in ctx
    assert "a" in ctx


def test_context_filters_high_sensitivity_memory(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通记忆内容", sensitivity=0)
    manager.write("personal", "高敏记忆机密", sensitivity=2)
    ctx = build_cloud_context("记忆", memory=manager)
    assert "普通记忆内容" in ctx
    assert "高敏记忆机密" not in ctx


def test_context_never_raises_with_nothing():
    ctx = build_cloud_context("", situation=None, memory=None)
    assert "用户说" in ctx


def test_context_omits_empty_memory_section(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    ctx = build_cloud_context("无匹配内容xyz", memory=manager)
    assert "相关记忆" not in ctx
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_context.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.l2.context'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/l2/context.py`**

```python
from yuki.memory.manager import MemoryManager

CLOUD_MEMORY_TOP_K = 3


def build_cloud_context(
    utterance: str,
    situation: dict | None = None,
    memory: MemoryManager | None = None,
) -> str:
    """组装发往云端的纯文本上下文。仅文本（不含帧/音频）；记忆过滤 sensitivity==2。"""
    parts = [f"用户说：{utterance or ''}"]
    if situation:
        topic = situation.get("topic", "") or ""
        summary = situation.get("summary", "") or ""
        points = situation.get("key_points") or []
        bits = [b for b in [topic, summary, *points] if b]
        if bits:
            parts.append("当前情境：" + " ".join(bits))
    if memory is not None:
        hits = memory.query(utterance or "", top_k=CLOUD_MEMORY_TOP_K, min_sensitivity=0)
        safe = [m for m in hits if m.get("sensitivity", 0) != 2]
        if safe:
            parts.append("相关记忆：\n" + "\n".join(f"- {m['content']}" for m in safe))
    return "\n".join(parts)
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_context.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/l2/context.py tests/cognition/l2/test_context.py
git commit -m "feat: add cloud context builder with sensitivity filtering"
```

---

### Task 3: CloudBridge（工具调用多轮）

**Files:**
- Create: `src/yuki/cognition/l2/bridge.py`
- Test: `tests/cognition/l2/test_bridge.py`

**Interfaces:**
- Consumes: `CloudClient`/`CloudError`（Task 1）、`build_cloud_context`（Task 2）、`FunctionRegistry`。
- Produces: `DEFAULT_PERSONA_PROMPT`（含 `{persona}` 占位）、`CloudBridge(client, registry=None, system_prompt=None, max_turns=3, persona_name="yuki")` 方法 `generate(utterance, situation=None, memory=None) -> str`。Task 5 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/l2/test_bridge.py`**

```python
import pytest

from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudError
from yuki.functions.registry import FunctionRegistry


class TurnClient:
    """按序返回预设响应，记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return self._responses.pop(0)


def test_generate_single_turn_text():
    client = TurnClient([{"choices": [{"message": {"content": "你好呀"}}]}])
    bridge = CloudBridge(client, registry=FunctionRegistry())
    out = bridge.generate("你好")
    assert out == "你好呀"
    assert client.calls[0][0][0]["role"] == "system"
    assert client.calls[0][0][1]["role"] == "user"
    assert client.calls[0][1] == []  # registry 无函数 → tools 为空列表


def test_generate_tool_call_loop():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    tool_response = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    ]}}]}
    client = TurnClient([tool_response, {"choices": [{"message": {"content": "最终回答"}}]}])
    bridge = CloudBridge(client, registry=registry)
    out = bridge.generate("测试")
    assert out == "最终回答"
    second_messages = client.calls[1][0]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["id"] == "c1"
    assert second_messages[-1]["role"] == "tool"
    assert "ok" in second_messages[-1]["content"]
    assert client.calls[0][1][0]["function"]["name"] == "echo"


def test_generate_empty_reply_raises():
    client = TurnClient([{"choices": [{"message": {"content": "   "}}]}])
    bridge = CloudBridge(client)
    with pytest.raises(CloudError):
        bridge.generate("x")


def test_generate_tool_loop_exhaustion_raises():
    registry = FunctionRegistry()
    registry.tool("echo", description="e", params=None)(lambda p: "ok")
    tool_response = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]}}]}
    client = TurnClient([tool_response] * 5)
    bridge = CloudBridge(client, registry=registry, max_turns=3)
    with pytest.raises(CloudError):
        bridge.generate("x")
    assert len(client.calls) == 3


def test_generate_missing_message_key_raises_cloud_error():
    def bad_chat(messages, tools=None):
        return {"choices": [{"foo": 1}]}

    bridge = CloudBridge(bad_chat)
    with pytest.raises(CloudError):
        bridge.generate("x")


def test_persona_prompt_contains_persona_name():
    from yuki.cognition.l2.bridge import DEFAULT_PERSONA_PROMPT
    assert "{persona}" in DEFAULT_PERSONA_PROMPT
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_bridge.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.l2.bridge'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/l2/bridge.py`**

```python
import json

from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.context import build_cloud_context
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

DEFAULT_PERSONA_PROMPT = (
    "你是{persona}，一个温柔的中文语音陪伴 agent。"
    "回复简短自然（1-3 句），贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时，优先表达关怀并建议求助。"
    "可以用工具查询记忆，但不要捏造记忆内容。"
)


class CloudBridge:
    """L2 云桥：请求构建 + 工具调用多轮 + 失败抛 CloudError。"""

    def __init__(
        self,
        client: CloudClient,
        registry: FunctionRegistry | None = None,
        system_prompt: str | None = None,
        max_turns: int = 3,
        persona_name: str = "yuki",
    ) -> None:
        self._client = client
        self._registry = registry
        self._max_turns = max_turns
        self._system = (system_prompt or DEFAULT_PERSONA_PROMPT).format(persona=persona_name)

    def generate(
        self,
        utterance: str,
        situation: dict | None = None,
        memory: MemoryManager | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": build_cloud_context(utterance, situation, memory)},
        ]
        tools = self._registry.tool_schemas() if self._registry else None
        try:
            for _ in range(self._max_turns):
                response = self._client.chat(messages, tools=tools)
                message = response["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = (message.get("content") or "").strip()
                    if not content:
                        raise CloudError("empty assistant reply")
                    return content
                messages.append({"role": "assistant", "content": message.get("content") or "",
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    fn = call.get("function") or {}
                    raw_args = fn.get("arguments", "{}")
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        arguments = {}
                    if self._registry is not None:
                        result = self._registry.dispatch({
                            "name": fn.get("name", ""), "arguments": arguments})
                    else:
                        result = {"ok": False, "error": {"message": "no registry"}}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            raise CloudError(f"tool loop exceeded max_turns={self._max_turns}")
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"generate failed: {exc}") from exc
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_bridge.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/l2/bridge.py tests/cognition/l2/test_bridge.py
git commit -m "feat: add CloudBridge with multi-turn tool calling"
```

---

### Task 4: 记忆函数绑定（memory_tools.py）

**Files:**
- Create: `src/yuki/functions/memory_tools.py`
- Test: `tests/functions/test_memory_tools.py`

**Interfaces:**
- Consumes: `FunctionRegistry`、`MemoryManager`。
- Produces: `register_memory_functions(registry, manager)`，注册 `memory.query/write/list/get`（pydantic 参数模型）。Task 6 依赖。

- [ ] **Step 1: 写失败测试 `tests/functions/test_memory_tools.py`**

```python
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_registers_four_functions(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    assert set(registry.names()) == {"memory.query", "memory.write", "memory.list", "memory.get"}


def test_query_filters_high_sensitivity(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通记忆内容", sensitivity=0)
    manager.write("personal", "高敏机密", sensitivity=2)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    results = registry.call("memory.query", {"text": "记忆"})
    contents = [r["content"] for r in results]
    assert "普通记忆内容" in contents
    assert "高敏机密" not in contents


def test_write_and_get_roundtrip(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    rid = registry.call("memory.write", {"memory_type": "preference", "content": "喜欢猫"})["id"]
    got = registry.call("memory.get", {"id": rid})["memory"]
    assert got["content"] == "喜欢猫"


def test_get_high_sensitivity_returns_none(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    rid = manager.write("personal", "高敏机密", sensitivity=2)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    assert registry.call("memory.get", {"id": rid})["memory"] is None


def test_query_params_schema_exportable(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    schemas = {s["function"]["name"]: s for s in registry.tool_schemas()}
    assert schemas["memory.query"]["function"]["parameters"]["type"] == "object"
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/functions/test_memory_tools.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.functions.memory_tools'`）。

- [ ] **Step 3: 创建 `src/yuki/functions/memory_tools.py`**

```python
from typing import Literal

from pydantic import BaseModel, Field

from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

MemoryType = Literal["preference", "personal", "scenario", "reflection"]


class QueryParams(BaseModel):
    text: str = Field(description="检索关键词")
    top_k: int = Field(5, ge=1, le=20)
    type: str | None = None
    min_sensitivity: int = Field(0, ge=0, le=2)


class WriteParams(BaseModel):
    memory_type: MemoryType
    content: str
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    sensitivity: int = Field(0, ge=0, le=2)
    source: str = "brain"
    metadata: dict = Field(default_factory=dict)


class ListParams(BaseModel):
    type: str | None = None
    min_sensitivity: int = Field(0, ge=0, le=2)


class GetParams(BaseModel):
    id: int


def _strip_high_sensitivity(results: list[dict]) -> list[dict]:
    return [m for m in results if m.get("sensitivity", 0) != 2]


def register_memory_functions(registry: FunctionRegistry, manager: MemoryManager) -> None:
    """绑定记忆函数。隐私硬约束：云端经工具也取不到 sensitivity==2 高敏记忆。"""

    def on_query(p: QueryParams) -> list:
        return _strip_high_sensitivity(
            manager.query(p.text, top_k=p.top_k, memory_type=p.type,
                          min_sensitivity=p.min_sensitivity))

    def on_write(p: WriteParams) -> dict:
        return {"id": manager.write(
            p.memory_type, p.content, confidence=p.confidence,
            sensitivity=p.sensitivity, source=p.source, metadata=p.metadata)}

    def on_list(p: ListParams) -> list:
        return _strip_high_sensitivity(
            manager.list(memory_type=p.type, min_sensitivity=p.min_sensitivity))

    def on_get(p: GetParams) -> dict:
        mem = manager.get(p.id)
        if mem is None or mem.get("sensitivity", 0) == 2:
            return {"memory": None}
        return {"memory": mem}

    registry.tool("memory.query", description="检索记忆（高敏自动排除）", params=QueryParams)(on_query)
    registry.tool("memory.write", description="写入一条记忆", params=WriteParams)(on_write)
    registry.tool("memory.list", description="列出记忆（高敏自动排除）", params=ListParams)(on_list)
    registry.tool("memory.get", description="按 id 获取记忆（高敏返回 null）", params=GetParams)(on_get)
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/functions/test_memory_tools.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/functions/memory_tools.py tests/functions/test_memory_tools.py
git commit -m "feat: bind memory query/write/list/get as functions"
```

---

### Task 5: 层级选择 + DecisionHub 集成

**Files:**
- Modify: `src/yuki/cognition/brain/policy.py`（Tier + tier_for + L2_INTENTS）、`tests/cognition/test_policy.py`
- Modify: `src/yuki/cognition/brain/hub.py`（bridge + tier 路由 + trace tier）、`tests/cognition/test_hub.py`
- Test: `tests/cognition/test_policy.py`、`tests/cognition/test_hub.py`

**Interfaces:**
- Consumes: `CloudBridge`/`CloudError`（Task 3）、现有 policy/hub。
- Produces: `Tier(Enum)`；`DecisionPolicy.tier_for(intent) -> Tier`；`DecisionHub.__init__` 增 `bridge=None`；`DecisionTrace` 增 `tier`；`build_brain(..., bridge=None)`。Task 6 依赖。

- [ ] **Step 1: 追加 tier 测试到 `tests/cognition/test_policy.py`**

```python
from yuki.cognition.brain.policy import DecisionPolicy, Tier, TriggerKind


def test_tier_for_mapping():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert policy.tier_for(Intent.ENTERTAINMENT) == Tier.L2
    assert policy.tier_for(Intent.CREATIVE) == Tier.L2
    assert policy.tier_for(Intent.ROLEPLAY) == Tier.L2
    assert policy.tier_for(Intent.GAME) == Tier.L2
    assert policy.tier_for(Intent.EMOTIONAL) == Tier.L2
    assert policy.tier_for(Intent.SAFETY) == Tier.L1
    assert policy.tier_for(Intent.CHIT_CHAT) == Tier.L1
    assert policy.tier_for(Intent.UNKNOWN) == Tier.L1
```

- [ ] **Step 2: 追加 hub L2 测试到 `tests/cognition/test_hub.py`**

在文件顶部 import 增补：

```python
from yuki.cognition.l2.client import CloudError
```

在文件末尾追加：

```python
class FakeBridge:
    def __init__(self, reply=None, error=None):
        self._reply = reply
        self._error = error
        self.calls = []

    def generate(self, utterance, situation=None, memory=None):
        self.calls.append(utterance)
        if self._error:
            raise self._error
        return self._reply


def test_l2_intent_routes_to_bridge(hub):
    h, bus, _ = hub
    h._bridge = FakeBridge(reply="云端深度回答")
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus) == "云端深度回答"


def test_l2_failure_falls_back_to_l1(hub):
    h, bus, _ = hub
    h._bridge = FakeBridge(error=CloudError("boom"))
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus)  # L1 动作链兜底，有回复


def test_l2_intent_without_bridge_uses_l1(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus)


def test_l1_intent_never_calls_bridge(hub):
    h, bus, _ = hub
    bridge = FakeBridge(reply="不应被调用")
    h._bridge = bridge
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert bridge.calls == []


def test_decision_trace_includes_tier(hub):
    h, bus, _ = hub
    records = []
    h._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert records[0]["tier"] == "l2"
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py tests/cognition/test_hub.py -v`
Expected: FAIL（`AttributeError: 'DecisionPolicy' object has no attribute 'tier_for'` / `TypeError: DecisionTrace ... tier`）。

- [ ] **Step 4: `src/yuki/cognition/brain/policy.py` 追加 Tier / tier_for / L2_INTENTS**

在 `TriggerKind` 定义后追加：

```python
class Tier(str, Enum):
    L1 = "l1"
    L2 = "l2"
```

在 `DEFAULT_POLICY_TABLE` 后追加：

```python
L2_INTENTS = {Intent.ENTERTAINMENT, Intent.CREATIVE, Intent.ROLEPLAY, Intent.GAME, Intent.EMOTIONAL}
```

在 `DecisionPolicy` 类中、`decide` 之前追加：

```python
    def tier_for(self, intent: Intent) -> Tier:
        return Tier.L2 if intent in L2_INTENTS else Tier.L1
```

- [ ] **Step 5: 重写 `src/yuki/cognition/brain/hub.py`**

```python
import time

from yuki.cognition.brain.actions import ACTION_EXECUTORS, ActionContext
from yuki.cognition.brain.classifier import (
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)
from yuki.cognition.brain.policy import DecisionPolicy, Tier, TriggerKind
from yuki.cognition.l1 import L1Engine
from yuki.cognition.l2.client import CloudError
from yuki.logger import get_decision_logger
from yuki.topics import Topics


class DecisionTrace:
    def __init__(self, *, ts, trigger, intent, emotion, actions, rendered, reason,
                 tier, cooldown_state) -> None:
        self.ts = ts
        self.trigger = trigger
        self.intent = intent
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.tier = tier
        self.cooldown_state = cooldown_state

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "intent": self.intent,
            "emotion": self.emotion,
            "actions": [a.name for a in self.actions],
            "rendered": self.rendered,
            "reason": self.reason,
            "tier": self.tier,
            "cooldown_state": self.cooldown_state,
        }


class DecisionHub:
    """Brain 内核：分类 → tier 路由（L2 云桥 / L1 动作链）→ 执行 → 发布 REPLY + 轨迹。"""

    def __init__(self, bus, *, intent_clf=None, emotion_clf=None, policy=None,
                 memory=None, registry=None, l1=None, executors=None, trace_logger=None,
                 bridge=None) -> None:
        self._bus = bus
        self._intent_clf = intent_clf or RuleIntentClassifier()
        self._emotion_clf = emotion_clf or RuleEmotionClassifier()
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._l1 = l1 or L1Engine()
        self._executors = executors if executors is not None else ACTION_EXECUTORS
        self._trace_logger = trace_logger or get_decision_logger()
        self._bridge = bridge
        self._context = None
        self._last_open_ts = None

    def on_situation_update(self, topic: str, payload: dict) -> None:
        self._context = payload
        self._handle(TriggerKind.SITUATION, "", situation=payload)

    def on_awake(self, topic: str, payload: dict) -> None:
        self._handle(TriggerKind.AWAKE, "")

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        self._handle(TriggerKind.UTTERANCE, text)

    def _handle(self, trigger: TriggerKind, text: str, situation: dict | None = None) -> None:
        intent = Intent.UNKNOWN
        emotion = Emotion.NEUTRAL
        tier = Tier.L1
        if trigger == TriggerKind.UTTERANCE:
            intent = self._intent_clf.classify(text)
            emotion = self._emotion_clf.classify(text)
            tier = self._policy.tier_for(intent)
        rendered, spoke, reason = "", False, "silent"
        if tier == Tier.L2 and self._bridge is not None:
            rendered, spoke = self._try_l2(text, situation or self._context)
            if spoke:
                reason = "l2"
        if not spoke:
            actions = self._policy.decide(
                trigger, intent, emotion, text=text, situation=situation or self._context,
                last_open_ts=self._last_open_ts, now=time.time(),
            )
            rendered, spoke = self._execute(actions, intent, emotion, text, situation or self._context)
            reason = "silent" if not spoke else "l1"
        if spoke:
            self._last_open_ts = time.time()
            self._bus.publish(Topics.REPLY, {"text": rendered, "ts": time.time()})
        self._trace_logger.info("decision", **DecisionTrace(
            ts=time.time(), trigger=trigger.value, intent=intent.value, emotion=emotion.value,
            actions=[], rendered=rendered, reason=reason, tier=tier.value,
            cooldown_state={"last_open_ts": self._last_open_ts},
        ).to_dict())

    def _try_l2(self, text: str, situation: dict | None):
        try:
            reply = self._bridge.generate(text, situation, self._memory)
        except CloudError:
            return "", False
        reply = (reply or "").strip()
        if not reply:
            return "", False
        return reply, True

    def _execute(self, actions, intent, emotion, text, situation):
        ctx = ActionContext(intent=intent, emotion=emotion, text=text,
                            situation=situation, memory=self._memory,
                            registry=self._registry, l1=self._l1)
        fragments = []
        for action in actions:
            executor = self._executors.get(action.name)
            if executor is None:
                continue
            fragments.append(executor(action, ctx))
        rendered = " ".join(f for f in fragments if f)
        return rendered, bool(rendered)


def build_brain(bus, *, memory=None, registry=None, config=None,
                intent_clf=None, emotion_clf=None, policy=None, bridge=None) -> DecisionHub:
    from yuki.config import Config
    cfg = config or Config.from_env()
    hub = DecisionHub(
        bus,
        intent_clf=intent_clf,
        emotion_clf=emotion_clf,
        policy=policy or DecisionPolicy(
            proactive_cooldown_s=cfg.brain.proactive_cooldown_s,
            proactive_enabled=cfg.brain.proactive_enabled,
        ),
        memory=memory,
        registry=registry,
        bridge=bridge,
    )
    bus.subscribe(Topics.AWAKE, hub.on_awake)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
```

注意：`DecisionTrace` 的 `actions` 参数现在恒传 `[]`（L2 路径无动作；L1 路径在 trace 中只记录空列表，动作已体现在 rendered）。若你更想保留 L1 的 actions 到 trace，可自行把 `actions` 变量在 L1 分支收集后传入——但保持与上述代码一致即可。

- [ ] **Step 6: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py tests/cognition/test_hub.py -v`
Expected: 全 PASS（既有 hub 测试不受影响：bridge 默认 None → 全走 L1）。

- [ ] **Step 7: Commit**

```bash
git add src/yuki/cognition/brain/policy.py src/yuki/cognition/brain/hub.py tests/cognition/test_policy.py tests/cognition/test_hub.py
git commit -m "feat: add tier routing to DecisionHub with L2 bridge fallback"
```

---

### Task 6: 接线 CognitionAgent + 健康 + 全仓回归

**Files:**
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Create: `src/yuki/cognition/l2/__init__.py`（补导出）
- Test: `tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `CloudBridge`/`CloudClient`（Task 3）、`register_memory_functions`（Task 4）、`build_brain`（Task 5）。
- Produces: `CognitionAgent.setup()` 构建 registry 后绑定记忆函数；`cloud.enabled` 时构建 `CloudBridge` 传入 `build_brain`；`health_components()` 增 `l2`。`l2/__init__.py` 导出 `CloudBridge`/`CloudClient`/`CloudError`/`build_cloud_context`。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_cognition.py`**

```python
def test_cognition_agent_registers_memory_functions_and_l2_health(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert "memory.query" in agent._registry.names()
        components = agent.health_components()
        assert "l2" in components
        assert components["l2"]().ok is True  # 未启用视为正常
    finally:
        agent.teardown()
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_cognition.py -v`
Expected: FAIL（`AttributeError: 'CognitionAgent' object has no attribute '_registry'` 或 health 无 `l2`）。

- [ ] **Step 3: 修改 `src/yuki/cognition/agent.py`**

顶部 import 增补：

```python
import os

from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudClient
from yuki.functions.memory_tools import register_memory_functions
```

在 `setup()` 中，`register_builtin_system(self._registry)` 之后追加：

```python
        register_memory_functions(self._registry, self._memory)
        bridge = None
        if self.config.cloud.enabled:
            bridge = CloudBridge(
                CloudClient(
                    base_url=self.config.cloud.base_url,
                    model=self.config.cloud.model,
                    api_key=os.environ.get(self.config.cloud.api_key_env),
                    timeout_s=self.config.cloud.timeout_s,
                ),
                registry=self._registry,
                max_turns=self.config.cloud.max_turns,
                persona_name=self.config.persona_name,
            )
        self._bridge = bridge
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            bridge=bridge,
        )
```

（原 `self._hub = build_brain(...)` 块替换为上述，含 `bridge=bridge`。）

在 `health_components()` 中 `"brain"` 之后增补：

```python
            "l2": self._health_l2,
```

在 `_health_brain` 之后追加：

```python
    def _health_l2(self) -> HealthStatus:
        enabled = self.config.cloud.enabled
        ok = (not enabled) or self._bridge is not None
        return HealthStatus(ok, {"enabled": enabled, "installed": self._bridge is not None})
```

- [ ] **Step 4: 更新 `src/yuki/cognition/l2/__init__.py`**

```python
from yuki.cognition.l2.bridge import CloudBridge  # noqa: F401
from yuki.cognition.l2.client import CloudClient, CloudError  # noqa: F401
from yuki.cognition.l2.context import build_cloud_context  # noqa: F401
```

- [ ] **Step 5: 运行全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（cloud 默认关，行为不变；新增 l2 测试通过）。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: wire CloudBridge into CognitionAgent and add l2 health"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1-6；§3 CloudClient → Task 1；§4 context → Task 2；§5 CloudBridge（工具多轮/降级）→ Task 3；§6 persona → Task 3；§7 tier_for → Task 5；§8 hub 集成（路由/回退/轨迹 tier）→ Task 5；§9 memory_tools（隐私硬约束）→ Task 4；§10 配置/隐私 → Task 1/6；§11 健康与测试 → Task 6 + 各任务。
- **一致性**：`CloudBridge.generate(utterance, situation, memory)` 在 Task 3 定义、Task 5 hub `_try_l2` 消费；`register_memory_functions` 在 Task 4 定义、Task 6 接线；`CloudConfig` 字段名与 env `YUKI_CLOUD_*` 一致；`tier_for` 在 Task 5 定义、hub/测试一致；`DecisionTrace` 增 `tier` 与测试一致。
- **隐私**：context 过滤 + memory_tools 硬过滤 `sensitivity==2`，两处一致。
- **e2e 等价**：cloud 默认关 → bridge None → 恒 L1；awake → `我在,你说。` 不变。
