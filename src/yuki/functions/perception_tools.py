from __future__ import annotations

from yuki.functions.registry import FunctionRegistry, RateLimit
from yuki.functions.screen import ScreenQueryPort
from yuki.perception.system_monitor import ForegroundProbe

VISION_UNDERSTAND_LIMIT = RateLimit(max_calls=3, window_seconds=60.0)


def register_perception_tools(
    registry: FunctionRegistry,
    screen: ScreenQueryPort,
    *,
    foreground_probe: ForegroundProbe | None = None,
) -> None:
    probe = foreground_probe or ForegroundProbe()

    if "window.info" not in registry.names():

        @registry.tool(
            "window.info",
            description="Return metadata for the current foreground window.",
            params=None,
            cost="light",
        )
        def _window_info(params=None):
            result = probe.probe()
            if result is None:
                return {
                    "app": "",
                    "url": "",
                    "title": "",
                    "hwnd": None,
                    "degraded": True,
                    "reason": "no_foreground_window",
                }
            return result

    if "screen.capture" not in registry.names():

        @registry.tool(
            "screen.capture",
            description="Return the latest captured screen frame and metadata.",
            params=None,
            cost="light",
        )
        def _screen_capture(params=None):
            return screen.latest_frame()

    if "text.extract" not in registry.names():

        @registry.tool(
            "text.extract",
            description="Extract current screen text evidence from DOM, UIA, or OCR providers.",
            params=None,
            cost="light",
        )
        def _text_extract(params=None):
            return screen.current_text()

    if "vision.understand" not in registry.names():

        @registry.tool(
            "vision.understand",
            description="Run deep visual understanding for the current screen.",
            params=None,
            cost="heavy",
            rate_limit=VISION_UNDERSTAND_LIMIT,
        )
        def _vision_understand(params=None):
            return screen.understand_screen_deep(bypass_rate_limit=True)
