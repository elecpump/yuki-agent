from yuki.cognition.brain.soul import SoulStore
from yuki.functions.registry import FunctionRegistry
from yuki.functions.soul_tools import register_soul_functions


def test_soul_update_tool_schema_and_prompt_refresh_callback(tmp_path):
    registry = FunctionRegistry()
    store = SoulStore(tmp_path / "soul.json", "yuki", min_snapshot_interval_s=0)
    store.ensure()
    refreshed = []
    register_soul_functions(registry, store, on_updated=lambda: refreshed.append(True))

    schema = registry.get_tool_schema("soul.update")["function"]["parameters"]
    assert set(schema["properties"]) == {"traits", "core_values", "description"}
    assert "source" not in schema["properties"]

    result = registry.dispatch({
        "name": "soul.update",
        "arguments": {"traits": {"warmth": 0.8}},
    })

    assert result["ok"] is True
    assert result["result"] == {"updated": True}
    assert refreshed == [True]

    noop = registry.dispatch({
        "name": "soul.update",
        "arguments": {"traits": {"warmth": 0.8}},
    })
    assert noop["ok"] is True
    assert noop["result"] == {"updated": False}
    assert refreshed == [True]


def test_soul_update_tool_rejects_extra_fields_and_invalid_core_value(tmp_path):
    registry = FunctionRegistry()
    store = SoulStore(tmp_path / "soul.json", "yuki")
    register_soul_functions(registry, store)

    extra = registry.dispatch({
        "name": "soul.update",
        "arguments": {"description": "new", "source": "periodic"},
    })
    invalid_role = registry.dispatch({
        "name": "soul.update",
        "arguments": {
            "core_values": [{"id": "cv.x", "text": "x", "role": "other"}],
        },
    })
    empty_values = registry.dispatch({
        "name": "soul.update",
        "arguments": {"core_values": []},
    })
    duplicate_ids = registry.dispatch({
        "name": "soul.update",
        "arguments": {
            "core_values": [
                {"id": "cv.x", "text": "x", "role": "guiding"},
                {"id": "cv.x", "text": "y", "role": "binding"},
            ],
        },
    })

    assert extra["ok"] is False
    assert extra["error"]["code"] == "invalid_arguments"
    assert invalid_role["ok"] is False
    assert invalid_role["error"]["code"] == "invalid_arguments"
    assert empty_values["error"]["code"] == "invalid_arguments"
    assert duplicate_ids["error"]["code"] == "invalid_arguments"


def test_soul_update_tool_hardcodes_realtime_audit_source(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.cognition.brain.soul.get_audit_logger", lambda: FakeAudit())
    registry = FunctionRegistry()
    store = SoulStore(tmp_path / "soul.json", "yuki")
    register_soul_functions(registry, store)

    result = registry.dispatch({
        "name": "soul.update",
        "arguments": {"description": "新的人格描述"},
    })

    assert result["ok"] is True
    update = next(fields for event, fields in calls if event == "soul.update")
    assert update["source"] == "realtime"
