from yuki.functions.perception_tools import register_perception_tools
from yuki.functions.registry import FunctionRegistry


class FakePipeline:
    def __init__(self) -> None:
        self.deep_bypass_values = []

    def latest_frame(self) -> dict:
        return {"frame_id": 1, "png": "abc"}

    def current_text(self) -> dict:
        return {"source": "dom", "text": "hello"}

    def understand_screen_deep(self, *, bypass_rate_limit=None) -> dict:
        self.deep_bypass_values.append(bypass_rate_limit)
        return {"topic": "screen"}


class FakeProbe:
    def probe(self) -> dict:
        return {"app": "editor", "title": "Notes", "hwnd": 7}


class EmptyProbe:
    def probe(self):
        return None


def test_register_perception_tools_dispatches_to_pipeline_and_probe() -> None:
    registry = FunctionRegistry()
    pipeline = FakePipeline()

    register_perception_tools(registry, pipeline, foreground_probe=FakeProbe())

    assert registry.dispatch({"name": "window.info"})["result"]["app"] == "editor"
    assert registry.dispatch({"name": "screen.capture"})["result"]["frame_id"] == 1
    assert registry.dispatch({"name": "text.extract"})["result"]["text"] == "hello"
    assert registry.dispatch({"name": "vision.understand"})["result"]["topic"] == "screen"
    assert pipeline.deep_bypass_values == [True]


def test_window_info_degrades_when_probe_has_no_foreground_window() -> None:
    registry = FunctionRegistry()

    register_perception_tools(registry, FakePipeline(), foreground_probe=EmptyProbe())

    result = registry.dispatch({"name": "window.info"})
    assert result["ok"] is True
    assert result["result"] == {
        "app": "",
        "url": "",
        "title": "",
        "hwnd": None,
        "degraded": True,
        "reason": "no_foreground_window",
    }


def test_vision_understand_is_rate_limited_by_tool_manager() -> None:
    registry = FunctionRegistry()
    pipeline = FakePipeline()

    register_perception_tools(registry, pipeline, foreground_probe=FakeProbe())

    assert registry.dispatch({"name": "vision.understand"})["ok"] is True
    assert registry.dispatch({"name": "vision.understand"})["ok"] is True
    assert registry.dispatch({"name": "vision.understand"})["ok"] is True
    limited = registry.dispatch({"name": "vision.understand"})

    assert limited["ok"] is False
    assert limited["error"]["code"] == "rate_limited"
    assert pipeline.deep_bypass_values == [True, True, True]
