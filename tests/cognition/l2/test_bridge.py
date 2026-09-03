import json

import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudError
from yuki.cognition.l2.loop import AgentLoop
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


class TurnClient:
    """按序返回预设响应，记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, timeout_s=None):
        self.calls.append((messages, tools, timeout_s))
        return self._responses.pop(0)


def test_generate_single_turn_text():
    client = TurnClient([{"choices": [{"message": {"content": "你好呀"}}]}])
    bridge = CloudBridge(client, registry=FunctionRegistry())
    out = bridge.generate("你好")
    assert out == "你好呀"
    messages = client.calls[0][0]
    assert messages[0]["role"] == "system"
    assert any("memory.write" in item.get("content", "") for item in messages)
    assert next(item for item in messages if item["role"] == "user")
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


def test_generate_tool_results_do_not_send_private_memory_to_cloud(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "安静公开偏好", sensitivity=0)
    manager.write("preference", "安静私密偏好", sensitivity=1)
    manager.write("personal", "安静高敏资料", sensitivity=2)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    tool_response = {"choices": [{"message": {"content": "", "tool_calls": [
        {
            "id": "mem1",
            "type": "function",
            "function": {"name": "memory.query", "arguments": '{"text":"安静","top_k":10}'},
        }
    ]}}]}
    client = TurnClient([tool_response, {"choices": [{"message": {"content": "最终回答"}}]}])

    bridge = CloudBridge(client, registry=registry)
    bridge.generate("安静")

    tool_message = client.calls[1][0][-1]
    tool_result = json.loads(tool_message["content"])
    contents = [m["content"] for m in tool_result["result"]]
    assert contents == ["安静公开偏好"]


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


def test_generate_wraps_context_errors_as_cloud_error():
    client = TurnClient([{"choices": [{"message": {"content": "x"}}]}])
    bridge = CloudBridge(client)
    with pytest.raises(CloudError):
        bridge.generate("x", context=ContextSnapshot(situation={"topic": "x", "key_points": [123]}))
    assert client.calls == []


def test_persona_prompt_contains_persona_name():
    from yuki.cognition.l2.bridge import DEFAULT_PERSONA_PROMPT
    assert "{persona}" in DEFAULT_PERSONA_PROMPT


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
    user_message = next(item for item in client.calls[0][0] if item["role"] == "user")
    assert "用户说：你好" in user_message["content"]


def test_generate_uses_provided_system_prompt_as_is():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    bridge = CloudBridge(client, system_prompt="你好呀{persona}保持这样")  # 不做 .format
    bridge.generate("你好", context=None, memory=None)
    system_messages = [m for m in client.calls[0][0] if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"].startswith("你好呀{persona}保持这样")


def test_set_system_prompt_updates():
    client = TurnClient([{"choices": [{"message": {"content": "回答"}}]}])
    bridge = CloudBridge(client, system_prompt="初始")
    bridge.set_system_prompt("新的系统提示")
    assert isinstance(bridge.loop, AgentLoop)
    assert bridge.loop._system == "新的系统提示"
    bridge.generate("你好", context=None, memory=None)
    system_messages = [m for m in client.calls[0][0] if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"].startswith("新的系统提示")


def test_refine_persona_calls_client():
    client = TurnClient([{"choices": [{"message": {"content": "  润色后的文本  "}}]}])
    bridge = CloudBridge(client)
    out = bridge.refine_persona("原始描述")
    assert out == "润色后的文本"
    system_msg, user_msg = client.calls[0][0]
    assert system_msg["role"] == "system"
    assert "润色" in system_msg["content"]
    assert user_msg["content"] == "原始描述"
    assert client.calls[0][1] is None  # 精修不带 tools
    assert client.calls[0][2] == 5.0
