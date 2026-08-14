import pytest
from pydantic import BaseModel, Field

from yuki.functions.registry import (
    ArgumentValidationError,
    FunctionRegistry,
    RegistryError,
    ToolExecutionError,
    ToolNotFoundError,
)


class EchoParams(BaseModel):
    text: str = Field(description="要回显的文本")
    times: int = 1


@pytest.fixture()
def registry():
    r = FunctionRegistry()

    def echo(params: EchoParams) -> str:
        return params.text * params.times

    r.tool("echo", description="回显文本 times 次", params=EchoParams)(echo)
    return r


def test_register_and_call(registry):
    assert registry.call("echo", {"text": "ab", "times": 3}) == "ababab"


def test_call_coerces_types(registry):
    assert registry.call("echo", {"text": "x", "times": "3"}) == "xxx"


def test_call_missing_optional_arg_uses_default(registry):
    assert registry.call("echo", {"text": "y"}) == "y"


def test_call_unknown_tool_raises(registry):
    with pytest.raises(ToolNotFoundError):
        registry.call("nope", {})


def test_call_invalid_args_raises(registry):
    with pytest.raises(ArgumentValidationError):
        registry.call("echo", {"text": 123})


def test_call_handler_error_wraps(registry):
    def boom(params):
        raise RuntimeError("kaboom")

    registry.tool("boom", description="总是失败", params=None)(boom)
    with pytest.raises(ToolExecutionError, match="kaboom"):
        registry.call("boom")


def test_handler_raising_function_error_propagates_unwrapped(registry):
    def inner(p):
        raise ArgumentValidationError("inner error")

    registry.tool("badargs", description="raises a FunctionError", params=None)(inner)
    with pytest.raises(ArgumentValidationError, match="inner error"):
        registry.call("badargs")


def test_register_duplicate_raises(registry):
    with pytest.raises(RegistryError):
        registry.tool("echo", description="dup", params=EchoParams)(lambda p: "")


def test_names_sorted(registry):
    registry.tool("zeta", description="z", params=None)(lambda p: 1)
    assert registry.names() == ["echo", "zeta"]


def test_no_params_tool_ignores_args(registry):
    registry.tool("noop", description="无参", params=None)(lambda p: p)
    assert registry.call("noop", {"junk": 1}) is None


from yuki.functions import FunctionError, FunctionRegistry, register_builtin_system


def test_builtin_system_ping():
    r = FunctionRegistry()
    register_builtin_system(r)
    assert "system.ping" in r.names()
    result = r.call("system.ping")
    assert result["ok"] is True
    assert isinstance(result["ts"], float)


def test_builtin_system_ping_dispatchable():
    r = FunctionRegistry()
    register_builtin_system(r)
    schemas = r.tool_schemas()
    assert any(s["function"]["name"] == "system.ping" for s in schemas)
    out = r.dispatch({"name": "system.ping", "arguments": "{}"})
    assert out["ok"] is True
    assert isinstance(out["result"]["ts"], float)


def test_package_exports_errors():
    assert issubclass(FunctionError, Exception)
