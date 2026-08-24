from typing import Protocol


class ScreenQueryPort(Protocol):
    """Minimal screen-query surface used by perception tools."""

    def latest_frame(self) -> dict: ...

    def current_text(self) -> dict: ...

    def understand_screen_deep(self, *, bypass_rate_limit: bool | None = None) -> dict: ...
