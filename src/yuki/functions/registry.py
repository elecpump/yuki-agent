from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


class FunctionError(Exception):
    """函数调用框架基类异常。"""


class RegistryError(FunctionError):
    """注册冲突或非法注册。"""


class ToolNotFoundError(FunctionError):
    """调用了未注册的工具。"""


class ArgumentValidationError(FunctionError):
    """参数解析或校验失败。"""


class ToolExecutionError(FunctionError):
    """handler 执行时抛出的异常。"""


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    params: type[BaseModel] | None
    handler: Callable[[BaseModel | None], Any]


class FunctionRegistry:
    """函数注册表：注册 → 校验 → 分发/调用 → schema 导出。

    注册应在装配阶段完成，之后只读调用；call/dispatch 每次调用无共享状态，无需加锁。
    handler 的返回值必须 JSON 可序列化。
    """

    def __init__(self) -> None:
        self._tools: dict[str, FunctionTool] = {}

    def register(self, tool: FunctionTool) -> None:
        if tool.name in self._tools:
            raise RegistryError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        *,
        description: str,
        params: type[BaseModel] | None = None,
    ):
        def decorator(fn: Callable[[BaseModel | None], Any]) -> Callable[[BaseModel | None], Any]:
            self.register(FunctionTool(name=name, description=description, params=params, handler=fn))
            return fn

        return decorator

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: dict | None = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        validated = self._validate(tool, args)
        try:
            return tool.handler(validated)
        except FunctionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"{name} failed: {exc}") from exc

    def _validate(self, tool: FunctionTool, args: dict | None) -> BaseModel | None:
        if tool.params is None:
            return None
        try:
            return tool.params.model_validate(args or {})
        except ValidationError as exc:
            raise ArgumentValidationError(f"{tool.name}: {exc}") from exc
