from typing import Callable

from yuki.bus import MessageBus
from yuki.cognition.responder import make_reply
from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.topics import Topics


def build_cognition(bus: MessageBus) -> None:
    def on_awake(topic: str, payload: dict) -> None:
        bus.publish(Topics.REPLY, make_reply(payload))

    bus.subscribe(Topics.AWAKE, on_awake)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_cognition(bus)
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
