import time
from typing import Callable

from yuki.bus import MessageBus
from yuki.cognition.responder import make_reply
from yuki.config import Config
from yuki.topics import Topics


def build_cognition(bus: MessageBus) -> None:
    def on_awake(topic: str, payload: dict) -> None:
        bus.publish(Topics.REPLY, make_reply(payload))

    bus.subscribe(Topics.AWAKE, on_awake)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role="hub")
    build_cognition(bus)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
