import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from yuki.runtime_bus import RuntimeBusProtocol
from yuki.topics import Topics


@dataclass
class HealthStatus:
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


class HealthReporter:
    """进程级健康聚合：组件检查 + 心跳发布 + health/{process} REQ/REP。"""

    def __init__(
        self,
        bus: RuntimeBusProtocol,
        process: str,
        heartbeat_interval: float = 5.0,
    ) -> None:
        self._bus = bus
        self._process = process
        self._interval = heartbeat_interval
        self._components: dict[str, Callable[[], HealthStatus]] = {}
        self._started_at = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register_component(self, name: str, check: Callable[[], HealthStatus]) -> None:
        self._components[name] = check

    def collect(self) -> dict[str, Any]:
        components: dict[str, dict[str, Any]] = {}
        healthy = True
        for name, check in self._components.items():
            try:
                status = check()
            except Exception:
                status = HealthStatus(False, {"error": "check raised"})
            components[name] = {"ok": status.ok, "detail": status.detail}
            healthy = healthy and status.ok
        if hasattr(self._bus, "bus_health"):
            bus_health = self._bus.bus_health()
            components["bus"] = {
                "ok": bus_health.get("healthy", True),
                "detail": bus_health,
            }
            healthy = healthy and bus_health.get("healthy", True)
        return {
            "process": self._process,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 2),
            "error_count": self._bus.error_count,
            "healthy": healthy,
            "components": components,
        }

    def start(self) -> None:
        service = f"health/{self._process}"
        handler = lambda payload: self.collect()
        if hasattr(self._bus, "supports_response_lanes"):
            self._bus.respond(service, handler, lane="control")
        else:
            self._bus.respond(service, handler)
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                data = self.collect()
                self._bus.publish(Topics.HEARTBEAT, {
                    "process": data["process"],
                    "ts": time.time(),
                    "healthy": data["healthy"],
                    "components": data["components"],
                })
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
