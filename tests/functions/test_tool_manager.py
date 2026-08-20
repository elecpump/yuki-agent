import pytest
from pydantic import BaseModel

from yuki.functions.registry import FunctionRegistry, RateLimit, RateLimitedError


class EchoParams(BaseModel):
    text: str


def test_list_tools_includes_cost_and_rate_limit() -> None:
    registry = FunctionRegistry()
    limit = RateLimit(max_calls=2, window_seconds=10.0)
    registry.tool(
        "echo",
        description="echo",
        params=EchoParams,
        cost="heavy",
        rate_limit=limit,
    )(lambda params: params.text)

    assert registry.list_tools() == [
        {
            "name": "echo",
            "description": "echo",
            "cost": "heavy",
            "rate_limit": {"max_calls": 2, "window_seconds": 10.0},
        }
    ]


def test_get_tool_schema_returns_openai_function_shape() -> None:
    registry = FunctionRegistry()
    registry.tool("echo", description="echo", params=EchoParams)(lambda params: params.text)

    schema = registry.get_tool_schema("echo")

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"]["required"] == ["text"]


def test_dispatch_rate_limited_after_arguments_validate() -> None:
    now = 100.0
    registry = FunctionRegistry(clock=lambda: now)
    calls = []
    registry.tool(
        "echo",
        description="echo",
        params=EchoParams,
        cost="heavy",
        rate_limit=RateLimit(max_calls=1, window_seconds=60.0),
    )(lambda params: calls.append(params.text) or params.text)

    assert registry.dispatch({"name": "echo", "arguments": {"text": "a"}})["ok"] is True

    invalid = registry.dispatch({"name": "echo", "arguments": {}})
    assert invalid["error"]["code"] == "invalid_arguments"

    limited = registry.dispatch({"name": "echo", "arguments": {"text": "b"}})
    assert limited["ok"] is False
    assert limited["error"]["code"] == "rate_limited"
    assert limited["error"]["retry_after"] == 60.0
    assert calls == ["a"]


def test_call_rate_limited_raises() -> None:
    now = 0.0
    registry = FunctionRegistry(clock=lambda: now)
    registry.tool(
        "ping",
        description="ping",
        params=None,
        cost="heavy",
        rate_limit=RateLimit(max_calls=1, window_seconds=5.0),
    )(lambda params: "pong")

    assert registry.call("ping") == "pong"
    with pytest.raises(RateLimitedError) as excinfo:
        registry.call("ping")
    assert excinfo.value.retry_after == 5.0
