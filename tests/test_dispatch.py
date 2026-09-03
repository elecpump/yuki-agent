import json
import re

from pydantic import BaseModel, Field

from yuki.functions.registry import FunctionRegistry


class EchoParams(BaseModel):
    text: str = Field(description="要回显的文本")
    times: int = 1


def _boom(params):
    raise RuntimeError("handler boom")


def make_registry() -> FunctionRegistry:
    r = FunctionRegistry()
    r.tool("echo", description="回显", params=EchoParams)(lambda p: p.text * p.times)
    r.tool("boom", description="失败", params=None)(_boom)
    r.tool("noop", description="无参", params=None)(lambda p: {"done": True})
    return r


def test_dispatch_ok():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": json.dumps({"text": "ab", "times": 2})})
    assert result == {"ok": True, "result": "abab"}


def test_dispatch_arguments_as_dict_ok():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": {"text": "x", "times": 3}})
    assert result["ok"] is True
    assert result["result"] == "xxx"


def test_dispatch_arguments_missing_or_empty_ok():
    r = make_registry()
    assert r.dispatch({"name": "noop"}) == {"ok": True, "result": {"done": True}}
    assert r.dispatch({"name": "noop", "arguments": ""}) == {"ok": True, "result": {"done": True}}
    assert r.dispatch({"name": "noop", "arguments": "   "}) == {"ok": True, "result": {"done": True}}


def test_dispatch_unknown_tool():
    r = make_registry()
    result = r.dispatch({"name": "missing", "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_found"


def test_dispatch_name_not_string():
    r = make_registry()
    result = r.dispatch({"name": {"not": "a string"}, "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_found"


def test_dispatch_invalid_json():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": "{oops"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_non_object_json():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": "[1,2]"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_schema_violation():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": json.dumps({"text": 123})})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_required_field_missing_from_blank_arguments():
    r = make_registry()
    result = r.dispatch({"name": "echo"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert r.dispatch({"name": "echo", "arguments": ""})["error"]["code"] == "invalid_arguments"


def test_dispatch_non_string_arguments():
    r = make_registry()
    result = r.dispatch({"name": "echo", "arguments": 123})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_dispatch_handler_error():
    r = make_registry()
    result = r.dispatch({"name": "boom", "arguments": "{}"})
    assert result["ok"] is False
    assert result["error"]["code"] == "handler_error"
    assert "handler boom" in result["error"]["message"]


def test_tool_schemas_shape():
    r = make_registry()
    schemas = r.tool_schemas()
    by_name = {s["function"]["name"]: s for s in schemas}
    assert set(by_name) == {"echo", "boom", "noop"}
    echo = by_name["echo"]["function"]
    assert by_name["echo"]["type"] == "function"
    assert echo["description"] == "回显"
    assert echo["parameters"]["type"] == "object"
    assert "text" in echo["parameters"]["properties"]
    assert echo["parameters"]["required"] == ["text"]
    assert by_name["noop"]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_dotted_tool_name_uses_wire_name_in_schemas_and_dispatch():
    r = FunctionRegistry()
    r.tool("memory.write", description="写入记忆", params=None)(lambda p: {"ok": True})
    r.tool("echo", description="回显", params=None)(lambda p: p or {"ok": True})

    schemas = r.tool_schemas()
    assert "memory.write" in {s["function"]["name"] for s in schemas}

    wire = r.tool_schemas(wire_names=True)
    names = {s["function"]["name"] for s in wire}
    assert "memory.write" not in names
    assert "memory_write" in names
    for s in wire:
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", s["function"]["name"])

    # 内部名与 wire 名都能 dispatch
    assert r.dispatch({"name": "memory.write"})["ok"] is True
    assert r.dispatch({"name": "memory_write"})["ok"] is True
    assert r.dispatch({"name": "echo"})["ok"] is True
