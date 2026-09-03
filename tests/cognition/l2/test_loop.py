import pytest

from yuki.cognition.l2.client import CloudError
from yuki.cognition.l2.loop import AgentLoop, CRISIS_SYSTEM_PROMPT, make_summarize
from yuki.functions.registry import FunctionRegistry


class TurnClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict], list[dict] | None, float | None]] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        self.calls.append((messages, tools, timeout_s))
        return self._responses.pop(0)


def _message(content: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _tool_call(arguments: str = "{}") -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": "echo", "arguments": arguments},
    }


def test_run_returns_single_final_reply() -> None:
    client = TurnClient([_message("你好呀")])
    loop = AgentLoop(client, FunctionRegistry(), system_prompt="你是测试助手")

    result = loop.run("你好")

    assert result == {
        "text": "你好呀",
        "steps": 1,
        "interrupted": False,
        "failed": False,
    }
    messages, tools, timeout_s = client.calls[0]
    # 云 API（如 DeepSeek）拒绝多个 system 消息：主提示与偏好记忆指令必须合并为一条。
    system_messages = [m for m in messages if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"].startswith("你是测试助手")
    assert "memory.write" in system_messages[0]["content"]
    assert tools == []
    assert timeout_s is not None and 0 < timeout_s <= 15.0


def test_run_executes_tool_and_returns_later_final_reply() -> None:
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("最终回答"),
    ])
    loop = AgentLoop(client, registry, system_prompt="测试")

    result = loop.run("查一下")

    assert result["text"] == "最终回答"
    assert result["steps"] == 2
    second_messages = client.calls[1][0]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-1]["role"] == "tool"
    assert '"ok": true' in second_messages[-1]["content"]


def test_loop_dispatches_wire_named_tool_call_to_dotted_tool() -> None:
    registry = FunctionRegistry()
    calls: list[str] = []
    registry.tool("memory.write", description="写入记忆", params=None)(
        lambda _: calls.append("called") or {"ok": True}
    )
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "memory_write", "arguments": "{}"},
    }
    client = TurnClient([_message("", [tool_call]), _message("写好了")])
    loop = AgentLoop(client, registry, system_prompt="测试")

    result = loop.run("记住我喜欢蓝色")

    assert result["text"] == "写好了"
    assert calls == ["called"]
    # 发给云 API 的工具 schema 必须使用 wire 名（点号会被 OpenAI 兼容 API 拒绝）
    schemas = {s["function"]["name"] for s in client.calls[0][1] or []}
    assert schemas == {"memory_write"}


def test_crisis_blocks_hallucinated_tool_call_without_dispatch() -> None:
    dispatched: list[str] = []
    transitions: list[str] = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(
        lambda _: dispatched.append("echo") or "ok"
    )
    client = TurnClient([
        _message("让我查一下", [_tool_call()]),
        _message("请立即联系身边可信任的人。"),
    ])
    loop = AgentLoop(client, registry, system_prompt="普通提示")

    result = loop.run(
        "我不想活了",
        crisis=True,
        on_transition=transitions.append,
    )

    assert result["text"] == "请立即联系身边可信任的人。"
    assert dispatched == []
    assert transitions == []
    assert client.calls[0][0][0]["content"] == CRISIS_SYSTEM_PROMPT
    assert client.calls[0][1] is None
    assert "crisis_tool_calls_blocked" in client.calls[1][0][-1]["content"]


def test_invalid_tool_arguments_return_error_without_dispatch() -> None:
    dispatched: list[str] = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(
        lambda _: dispatched.append("echo") or "ok"
    )
    client = TurnClient([
        _message("", [_tool_call("{invalid")]),
        _message("参数有误，无法执行。"),
    ])
    loop = AgentLoop(client, registry, system_prompt="测试")

    result = loop.run("执行")

    assert result["text"] == "参数有误，无法执行。"
    assert dispatched == []
    assert "invalid_tool_arguments" in client.calls[1][0][-1]["content"]


def test_tool_loop_emits_only_one_transition() -> None:
    transitions: list[str] = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("第一次查询", [_tool_call()]),
        _message("第二次查询", [_tool_call()]),
        _message("完成"),
    ])
    loop = AgentLoop(client, registry, system_prompt="测试")

    result = loop.run("查询", on_transition=transitions.append)

    assert result["text"] == "完成"
    assert transitions == ["第一次查询"]


def test_interrupt_during_model_call_discards_final_reply() -> None:
    interrupted = False

    class InterruptingClient(TurnClient):
        def chat(self, messages, tools=None, timeout_s=None):
            nonlocal interrupted
            response = super().chat(messages, tools=tools, timeout_s=timeout_s)
            interrupted = True
            return response

    client = InterruptingClient([_message("过期回复")])
    loop = AgentLoop(client, FunctionRegistry(), system_prompt="测试")

    result = loop.run("你好", interrupt_check=lambda: interrupted)

    assert result == {
        "text": "",
        "steps": 0,
        "interrupted": True,
        "failed": False,
    }


def test_interrupt_at_final_return_boundary_discards_stale_reply() -> None:
    checks = iter([False, False, True])
    client = TurnClient([_message("过期回复")])
    loop = AgentLoop(client, FunctionRegistry(), system_prompt="测试")

    result = loop.run("你好", interrupt_check=lambda: next(checks))

    assert result == {
        "text": "",
        "steps": 0,
        "interrupted": True,
        "failed": False,
    }


def test_tool_result_is_truncated_before_next_model_call() -> None:
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "长" * 5000)
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("完成"),
    ])
    loop = AgentLoop(
        client,
        registry,
        system_prompt="测试",
        tool_result_max_chars=100,
    )

    loop.run("查询")

    tool_content = client.calls[1][0][-1]["content"]
    assert len(tool_content) <= 120
    assert "已截断" in tool_content


def test_reply_returned_after_deadline_is_discarded() -> None:
    ticks = iter([0.0, 0.0, 0.0, 2.0])
    client = TurnClient([_message("过期回复")])
    loop = AgentLoop(
        client,
        FunctionRegistry(),
        system_prompt="测试",
        max_duration_s=1.0,
        clock=lambda: next(ticks),
    )

    result = loop.run("你好")

    assert result == {
        "text": "",
        "steps": 0,
        "interrupted": False,
        "failed": True,
    }
    assert client.calls[0][2] == 1.0


def test_compaction_folds_only_older_completed_tool_pairs() -> None:
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("", [_tool_call()]),
        _message("完成"),
    ])
    loop = AgentLoop(
        client,
        registry,
        system_prompt="测试",
        compact_threshold_tokens=1,
        summarize=lambda _: "已完成过程摘要",
    )

    result = loop.run("查询")

    assert result["text"] == "完成"
    final_messages = client.calls[2][0]
    summary_index = next(
        index
        for index, message in enumerate(final_messages)
        if "已完成过程摘要" in message.get("content", "")
    )
    assert final_messages[summary_index]["role"] == "user"
    assert final_messages[summary_index + 1]["role"] == "assistant"
    assert final_messages[summary_index + 2]["role"] == "tool"


def test_compaction_is_skipped_when_deadline_cannot_cover_summary_timeout() -> None:
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("", [_tool_call()]),
        _message("完成"),
    ])
    summaries: list[list[str]] = []
    loop = AgentLoop(
        client,
        registry,
        system_prompt="测试",
        max_duration_s=2.0,
        compact_threshold_tokens=1,
        summarize=lambda texts: summaries.append(texts) or "摘要",
        clock=lambda: 0.0,
    )

    result = loop.run("查询")

    assert result["text"] == "完成"
    assert summaries == []


def test_compaction_time_is_deducted_from_next_model_timeout() -> None:
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("", [_tool_call()]),
        _message("完成"),
    ])

    def summarize(_):
        clock.now += 2.0
        return "摘要"

    loop = AgentLoop(
        client,
        registry,
        system_prompt="测试",
        max_duration_s=10.0,
        compact_threshold_tokens=1,
        summarize=summarize,
        clock=clock,
    )

    loop.run("查询")

    assert client.calls[2][2] == 8.0


def test_interrupt_after_compaction_prevents_next_model_call() -> None:
    interrupted = False
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda _: "ok")
    client = TurnClient([
        _message("", [_tool_call()]),
        _message("", [_tool_call()]),
    ])

    def summarize(_):
        nonlocal interrupted
        interrupted = True
        return "摘要"

    loop = AgentLoop(
        client,
        registry,
        system_prompt="测试",
        max_duration_s=10.0,
        compact_threshold_tokens=1,
        summarize=summarize,
        clock=lambda: 0.0,
    )

    result = loop.run("查询", interrupt_check=lambda: interrupted)

    assert result["interrupted"] is True
    assert len(client.calls) == 2


def test_make_summarize_rejects_empty_summary() -> None:
    client = TurnClient([_message("   ")])

    with pytest.raises(CloudError, match="empty summary"):
        make_summarize(client)(["需要压缩的内容"])
