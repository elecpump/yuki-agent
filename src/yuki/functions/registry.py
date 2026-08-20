from yuki.functions.tool_manager import (
    ArgumentValidationError,
    Cost,
    FunctionError,
    FunctionTool,
    RateLimit,
    RateLimitedError,
    RateLimiter,
    RegistryError,
    ToolDefinition,
    ToolExecutionError,
    ToolManager,
    ToolNotFoundError,
)


class FunctionRegistry(ToolManager):
    pass


__all__ = [
    "ArgumentValidationError",
    "Cost",
    "FunctionError",
    "FunctionRegistry",
    "FunctionTool",
    "RateLimit",
    "RateLimitedError",
    "RateLimiter",
    "RegistryError",
    "ToolDefinition",
    "ToolExecutionError",
    "ToolManager",
    "ToolNotFoundError",
]
