import time

from yuki.functions.registry import FunctionRegistry


def register_builtin_system(registry: FunctionRegistry) -> None:
    """注册内置系统函数（当前仅 system.ping，作健康/演示）。"""

    @registry.tool("system.ping", description="健康/演示：无参数心跳，返回当前时间戳。")
    def _ping(params=None) -> dict:
        return {"ok": True, "ts": time.time()}
