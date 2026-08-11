from yuki.bus import MessageBus
from yuki.config import Config
from yuki.health import register_health_service
from yuki.shutdown import ShutdownManager


def build_perception(bus: MessageBus) -> None:
    """Phase 2 实现截屏/音频/系统监控。"""


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_perception(bus)
    register_health_service(bus, "perception")
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
