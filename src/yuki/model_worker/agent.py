from __future__ import annotations

from collections.abc import Callable

from yuki.bus import BusNode
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.model_worker.assembly import ModelWorkerRuntime, assemble_model_worker
from yuki.process import ProcessAgent
from yuki.runtime_bus import RuntimeBusProtocol
from yuki.shutdown import ShutdownManager


class ModelWorkerAgent(ProcessAgent):
    name = "model_worker"

    def __init__(
        self,
        config: Config,
        *,
        bus: RuntimeBusProtocol | None = None,
        shutdown: ShutdownManager | None = None,
    ) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self.runtime: ModelWorkerRuntime | None = None

    def setup(self) -> None:
        self.runtime = assemble_model_worker(self.config, self.bus)

    def _make_bus(self) -> RuntimeBusProtocol:
        return BusNode(
            base_port=self.config.bus.base_port,
            hwm=self.config.bus.hwm,
            auth_token=self.config.bus.auth_token,
            max_msg_size=self.config.bus.max_msg_size,
            register_interval=self.config.bus.register_interval_s,
        )

    def teardown(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    def health_components(self) -> dict[str, Callable[[], HealthStatus]]:
        def manager_health() -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            detail = self.runtime.manager.get_overall_status()
            return HealthStatus(bool(detail["healthy"]), detail)

        def manager_loop_health() -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            detail = self.runtime.manager.maintenance_snapshot()
            return HealthStatus(bool(detail["healthy"]), detail)

        def scheduler_health() -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            detail = self.runtime.scheduler.snapshot()
            return HealthStatus(bool(detail["healthy"]), detail)

        def operations_health() -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            detail = self.runtime.operations.snapshot()
            return HealthStatus(bool(detail["healthy"]), detail)

        def gpu_health() -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            detail = self.runtime.manager.gpu_health()
            return HealthStatus(True, detail)

        def model_health(model: str) -> HealthStatus:
            if self.runtime is None:
                return HealthStatus(True, {"state": "starting"})
            if model not in self.runtime.manager.names():
                return HealthStatus(True, {"state": "not_configured"})
            return HealthStatus(True, self.runtime.manager.get_model_health(model))

        components: dict[str, Callable[[], HealthStatus]] = {
            "manager": manager_health,
            "manager_loop": manager_loop_health,
            "scheduler": scheduler_health,
            "operations": operations_health,
            "gpu_runtime": gpu_health,
        }
        for model in ("vlm", "stt", "local_chat", "tts", "embedding"):
            components[f"model.{model}"] = (
                lambda model=model: model_health(model)
            )
        return components
