import os
import time

from yuki.bus import MessageBus


def register_health_service(bus: MessageBus, name: str) -> None:
    start = time.time()
    error_count = getattr(bus, "_error_count", 0)

    def handler(payload: dict) -> dict:
        return {
            "process": name,
            "pid": os.getpid(),
            "uptime_s": time.time() - start,
            "error_count": error_count,
        }

    bus.respond(f"health/{name}", handler)
