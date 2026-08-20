from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

Cost = Literal["light", "heavy"]


class FunctionError(Exception):
    """Base exception for tool/function calls."""


class RegistryError(FunctionError):
    """Raised when a tool cannot be registered."""


class ToolNotFoundError(FunctionError):
    """Raised when calling an unknown tool."""


class ArgumentValidationError(FunctionError):
    """Raised when tool arguments cannot be parsed or validated."""


class ToolExecutionError(FunctionError):
    """Raised when a tool handler fails."""


class RateLimitedError(FunctionError):
    """Raised when a tool exceeds its configured rate limit."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RateLimit:
    max_calls: int
    window_seconds: float


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    params: type[BaseModel] | None
    handler: Callable[[BaseModel | None], Any]
    cost: Cost = "light"
    rate_limit: RateLimit | None = None


FunctionTool = ToolDefinition


class RateLimiter:
    """Sliding-window limiter with independent counters per tool name."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, name: str, limit: RateLimit) -> tuple[bool, float]:
        now = self._clock()
        window = max(0.0, float(limit.window_seconds))
        max_calls = int(limit.max_calls)
        if max_calls <= 0:
            return False, window
        if window <= 0.0:
            return True, 0.0

        with self._lock:
            calls = self._calls.setdefault(name, deque())
            while calls and now - calls[0] >= window:
                calls.popleft()
            if len(calls) < max_calls:
                calls.append(now)
                return True, 0.0
            retry_after = max(0.0, window - (now - calls[0]))
            return False, retry_after


class ToolManager:
    """Register, validate, dispatch, and describe callable tools."""

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._rate_limiter = rate_limiter or RateLimiter(clock=clock)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise RegistryError(f"tool already registered: {tool.name!r}")
        if tool.cost not in ("light", "heavy"):
            raise RegistryError(f"invalid tool cost for {tool.name!r}: {tool.cost!r}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        *,
        description: str,
        params: type[BaseModel] | None = None,
        cost: Cost = "light",
        rate_limit: RateLimit | None = None,
    ):
        def decorator(fn: Callable[[BaseModel | None], Any]) -> Callable[[BaseModel | None], Any]:
            self.register(
                ToolDefinition(
                    name=name,
                    description=description,
                    params=params,
                    handler=fn,
                    cost=cost,
                    rate_limit=rate_limit,
                )
            )
            return fn

        return decorator

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: dict | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        validated = self._validate(tool, args)
        self._check_rate_limit(tool)
        try:
            return tool.handler(validated)
        except FunctionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"{name} failed: {exc}") from exc

    def dispatch(self, tool_call: dict) -> dict:
        name = tool_call.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return {
                "ok": False,
                "error": {"code": "tool_not_found", "message": f"unknown tool: {name!r}"},
            }
        try:
            args = self._parse_arguments(tool_call.get("arguments"))
        except ArgumentValidationError as exc:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        try:
            validated = self._validate(tool, args)
        except ArgumentValidationError as exc:
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        try:
            self._check_rate_limit(tool)
        except RateLimitedError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "rate_limited",
                    "message": str(exc),
                    "retry_after": exc.retry_after,
                },
            }
        try:
            result = tool.handler(validated)
        except Exception as exc:
            return {"ok": False, "error": {"code": "handler_error", "message": str(exc)}}
        return {"ok": True, "result": result}

    def tool_schemas(self) -> list[dict]:
        return [self.get_tool_schema(name) for name in self.names()]

    def list_tools(self) -> list[dict]:
        result = []
        for name in self.names():
            tool = self._tools[name]
            limit = tool.rate_limit
            result.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "cost": tool.cost,
                    "rate_limit": None
                    if limit is None
                    else {
                        "max_calls": limit.max_calls,
                        "window_seconds": limit.window_seconds,
                    },
                }
            )
        return result

    def get_tool_schema(self, name: str) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        if tool.params is None:
            parameters: dict = {"type": "object", "properties": {}}
        else:
            parameters = tool.params.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": parameters,
            },
        }

    def _check_rate_limit(self, tool: ToolDefinition) -> None:
        if tool.rate_limit is None:
            return
        allowed, retry_after = self._rate_limiter.allow(tool.name, tool.rate_limit)
        if allowed:
            return
        raise RateLimitedError(
            f"{tool.name} rate limited; retry after {retry_after:.3f}s",
            retry_after=retry_after,
        )

    def _parse_arguments(self, raw) -> dict:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ArgumentValidationError(f"arguments not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ArgumentValidationError("arguments must be a JSON object")
            return parsed
        raise ArgumentValidationError("arguments must be a string or object")

    def _validate(self, tool: ToolDefinition, args: dict | None) -> BaseModel | None:
        if tool.params is None:
            return None
        try:
            return tool.params.model_validate(args or {})
        except ValidationError as exc:
            raise ArgumentValidationError(f"{tool.name}: {exc}") from exc
