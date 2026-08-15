import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudError
from yuki.cognition.l2.view import CloudViewBuilder
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
    assert "用户说：你好" in client.calls[0][0][1]["content"]


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
