from abc import ABC, abstractmethod
from typing import Callable

from yuki.bus import BusNode
from yuki.config import Config
from yuki.health import HealthReporter, HealthStatus
from yuki.shutdown import ShutdownManager
from yuki.logger import configure_logging


class ProcessAgent(ABC):
    """进程生命周期框架：信号 → 健康 → setup → loop → teardown → 清理 → 关总线。"""

    name: str = "process"
    register_health: bool = True

    def __init__(
        self,
        config: Config,
        *,
        bus: BusNode | None = None,
        shutdown: ShutdownManager | None = None,
    ) -> None:
        self.config = config
        self.bus = bus or self._make_bus()
        self.shutdown = shutdown or ShutdownManager()
        self.health = HealthReporter(
            self.bus,
            process=self.name,
            heartbeat_interval=config.health.heartbeat_interval_s,
        )

    def _make_bus(self) -> BusNode:
        return BusNode(base_port=self.config.bus.base_port, hwm=self.config.bus.hwm)

    @abstractmethod
    def setup(self) -> None: ...

    @abstractmethod
    def teardown(self) -> None: ...

    def health_components(self) -> dict[str, Callable[[], HealthStatus]]:
        return {}

    def loop(self) -> None:
        while not self.shutdown.shutdown_requested:
            self.shutdown.wait(timeout=1.0)

    def run(self, *, register_signals: bool = True) -> None:
        configure_logging(self.config.logging.level, force=True)
        if register_signals:
            self.shutdown.register_signal_handlers()
        if self.register_health:
            for comp_name, check in self.health_components().items():
                self.health.register_component(comp_name, check)
            self.health.start()
        if hasattr(self.bus, "pause_subscriptions"):
            self.bus.pause_subscriptions()
        try:
            self.setup()
            if hasattr(self.bus, "resume_subscriptions"):
                self.bus.resume_subscriptions()
            self.loop()
        finally:
            try:
                self.teardown()
            finally:
                if self.register_health:
                    self.health.stop()
                self.shutdown.run_cleanups()
                self.bus.close()
