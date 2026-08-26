import pytest

from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.registry import ArgumentValidationError, FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_registers_four_functions(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    assert set(registry.names()) == {"memory.query", "memory.write", "memory.list", "memory.get"}


def test_query_filters_private_and_high_sensitivity(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通记忆内容", sensitivity=0)
    manager.write("preference", "私密记忆内容", sensitivity=1)
    manager.write("personal", "高敏记忆内容", sensitivity=2)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    results = registry.call("memory.query", {"text": "记忆"})
    contents = [r["content"] for r in results]
    assert "普通记忆内容" in contents
    assert "私密记忆内容" not in contents
    assert "高敏记忆内容" not in contents


def test_list_filters_private_and_high_sensitivity(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通记忆内容", sensitivity=0)
    manager.write("preference", "私密记忆内容", sensitivity=1)
    manager.write("personal", "高敏记忆内容", sensitivity=2)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    results = registry.call("memory.list", {})
    contents = [r["content"] for r in results]
    assert "普通记忆内容" in contents
    assert "私密记忆内容" not in contents
    assert "高敏记忆内容" not in contents


def test_write_rejects_invalid_memory_type(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    with pytest.raises(ArgumentValidationError):
        registry.call("memory.write", {"memory_type": "bogus", "content": "x"})


@pytest.mark.parametrize("sensitivity", [1, 2])
def test_write_rejects_non_public_sensitivity(tmp_path, sensitivity):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)

    with pytest.raises(ArgumentValidationError):
        registry.call(
            "memory.write",
            {
                "memory_type": "preference",
                "content": "敏感偏好",
                "sensitivity": sensitivity,
            },
        )


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


def test_get_private_sensitivity_returns_none(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    rid = manager.write("personal", "私密资料", sensitivity=1)
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    assert registry.call("memory.get", {"id": rid})["memory"] is None


def test_query_params_schema_exportable(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()
    register_memory_functions(registry, manager)
    schemas = {s["function"]["name"]: s for s in registry.tool_schemas()}
    assert schemas["memory.query"]["function"]["parameters"]["type"] == "object"
