from __future__ import annotations

import threading
from typing import Any

from yuki.bus import BusHub, BusNode
from yuki.bus_bridge import BusCompatibilityBridge
from yuki.bus_server.gateway import GatewayServer
from yuki.cognition.agent import CognitionAgent
from yuki.config import Config
from yuki.health import HealthReporter, HealthStatus
from yuki.interaction.agent import InteractionAgent
from yuki.model_client import (
    EmbeddingClient,
    LocalChatModelClient,
    RemoteModelRegistry,
    SttClient,
    TtsClient,
    VlmClient,
)
from yuki.perception.agent import PerceptionAgent
from yuki.process import ProcessAgent
from yuki.runtime_bus import LocalRuntimeBus, RuntimeBusProtocol
from yuki.shutdown import ShutdownManager


class YukiApp:
    def __init__(
        self,
        config: Config,
        *,
        hub: BusHub | None = None,
        remote_bus: RuntimeBusProtocol | None = None,
        local_bus: LocalRuntimeBus | None = None,
        shutdown: ShutdownManager | None = None,
        agents: list[ProcessAgent] | None = None,
        gateway: GatewayServer | None = None,
    ) -> None:
        self.config = config
        self.shutdown = shutdown or ShutdownManager()
        self.hub = hub or BusHub(
            base_port=config.bus.base_port,
            hwm=config.bus.hwm,
            auth_token=config.bus.auth_token,
            max_msg_size=config.bus.max_msg_size,
        )
        self.remote_bus = remote_bus or BusNode(
            base_port=config.bus.base_port,
            hwm=config.bus.hwm,
            auth_token=config.bus.auth_token,
            max_msg_size=config.bus.max_msg_size,
            register_interval=config.bus.register_interval_s,
        )
        self.local_bus = local_bus or LocalRuntimeBus(
            subscriber_queue_size=config.runtime_bus.subscriber_queue_size
        )
        self.bridge = BusCompatibilityBridge(
            self.local_bus,
            self.remote_bus,
            mirror_topic_prefixes=config.runtime_bus.mirror_topic_prefixes,
            mirror_queue_size=config.runtime_bus.mirror_queue_size,
        )
        self.health = HealthReporter(
            self.remote_bus,
            process="yuki",
            heartbeat_interval=config.health.heartbeat_interval_s,
        )
        self.agents = agents or self._build_agents()
        self.gateway = gateway
        self._loop_threads: list[threading.Thread] = []
        self._started = False

    def _build_agents(self) -> list[ProcessAgent]:
        vlm = VlmClient(self.remote_bus)
        stt = SttClient(self.remote_bus)
        local_chat = LocalChatModelClient(self.remote_bus)
        registry = RemoteModelRegistry(self.remote_bus)
        embedding = None
        if self.config.memory.vector_enabled:
            embedding = EmbeddingClient(
                self.remote_bus,
                model=self.config.memory.embedding_model,
                dimension=self.config.memory.embedding_dimension,
            )
        tts = TtsClient(self.remote_bus)
        return [
            PerceptionAgent(
                self.config,
                bus=self.local_bus,
                shutdown=self.shutdown,
            ),
            CognitionAgent(
                self.config,
                bus=self.local_bus,
                shutdown=self.shutdown,
                vlm=vlm,
                stt=stt,
                model_registry=registry,
                local_chat_model=local_chat,
                embedding_provider=embedding,
            ),
            InteractionAgent(
                self.config,
                bus=self.local_bus,
                shutdown=self.shutdown,
                tts_model=tts,
            ),
        ]

    def setup(self) -> None:
        if self._started:
            return
        self.local_bus.pause_subscriptions()
        setup_agents: list[ProcessAgent] = []
        try:
            for agent in self.agents:
                agent.setup()
                setup_agents.append(agent)
            self.bridge.start()
            self._register_health_components()
            self.health.start()
            self.local_bus.resume_subscriptions()
            for agent in self.agents:
                thread = threading.Thread(
                    target=agent.loop,
                    daemon=True,
                    name=f"yuki-app:{agent.name}",
                )
                thread.start()
                self._loop_threads.append(thread)
            if self.config.gateway.enabled:
                local_model_control = next(
                    (
                        control
                        for agent in self.agents
                        if (control := getattr(agent, "local_model_control", None)) is not None
                    ),
                    None,
                )
                self.gateway = self.gateway or GatewayServer(
                    self.config,
                    bus=self.local_bus,
                    hub=self.hub,
                    local_model_control=local_model_control,
                )
                self.gateway.start()
            self._started = True
        except Exception:
            for agent in reversed(setup_agents):
                try:
                    agent.teardown()
                except Exception:
                    pass
            raise

    def run(self) -> None:
        self.shutdown.register_signal_handlers()
        try:
            self.setup()
            while not self.shutdown.shutdown_requested:
                self.shutdown.wait(timeout=1.0)
        finally:
            self.close()

    def close(self) -> None:
        self.shutdown.request_shutdown()
        if self.gateway is not None:
            self.gateway.stop()
        for thread in self._loop_threads:
            thread.join(timeout=2.0)
        self._loop_threads.clear()
        for agent in reversed(self.agents):
            try:
                agent.teardown()
            except Exception:
                pass
        self.health.stop()
        self.bridge.close()
        self.local_bus.close()
        self.shutdown.run_cleanups()
        self.remote_bus.close()
        self.hub.close()
        self._started = False

    def _register_health_components(self) -> None:
        self.health.register_component(
            "bus_hub",
            lambda: _health_status(self.hub.health_snapshot()),
        )
        self.health.register_component(
            "local_runtime_bus",
            lambda: _health_status(self.local_bus.health()),
        )
        self.health.register_component(
            "remote_bus",
            lambda: _health_status(self.remote_bus.bus_health()),
        )
        self.health.register_component(
            "compatibility_bridge",
            lambda: _health_status(self.bridge.health()),
        )
        excluded = {
            "cognition.vlm",
            "cognition.stt",
            "cognition.models",
            "interaction.tts",
        }
        for agent in self.agents:
            for name, check in agent.health_components().items():
                qualified = f"{agent.name}.{name}"
                if qualified not in excluded:
                    self.health.register_component(qualified, check)
        self.health.register_component(
            "agent_loops",
            lambda: HealthStatus(
                all(thread.is_alive() for thread in self._loop_threads)
                if self._loop_threads
                else True,
                {
                    thread.name: thread.is_alive()
                    for thread in self._loop_threads
                },
            ),
        )


def _health_status(detail: dict[str, Any]) -> HealthStatus:
    return HealthStatus(bool(detail.get("healthy", True)), detail)


def main() -> None:
    config = Config.from_env()
    YukiApp(config).run()


if __name__ == "__main__":
    main()
