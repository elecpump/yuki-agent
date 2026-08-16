from pydantic import BaseModel

from tests.fakes import FakeBus
from yuki.functions.registry import FunctionRegistry
from yuki.functions.service import FUNCTIONS_CALL_SERVICE, register_function_services


class EchoParams(BaseModel):
    text: str
    times: int = 1


def _registry() -> FunctionRegistry:
    registry = FunctionRegistry()

    def echo(params: EchoParams) -> str:
        return params.text * params.times

    registry.tool("echo", description="回显文本", params=EchoParams)(echo)
    return registry


def test_function_service_dispatches_tool_calls() -> None:
    bus = FakeBus()
    register_function_services(bus, _registry())

    result = bus.request(
        FUNCTIONS_CALL_SERVICE,
        {"name": "echo", "arguments": {"text": "ha", "times": 2}},
    )

    assert result == {"ok": True, "result": "haha"}


def test_function_service_accepts_openai_json_arguments() -> None:
    bus = FakeBus()
    register_function_services(bus, _registry())

    result = bus.request(
        FUNCTIONS_CALL_SERVICE,
        {"name": "echo", "arguments": '{"text":"x","times":"3"}'},
    )

    assert result == {"ok": True, "result": "xxx"}


def test_function_service_returns_structured_errors() -> None:
    bus = FakeBus()
    register_function_services(bus, _registry())

    result = bus.request(
        FUNCTIONS_CALL_SERVICE,
        {"name": "missing", "arguments": {}},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_found"
