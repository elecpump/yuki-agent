import time

from yuki.bus import MessageBus
from yuki.config import Config


def build_perception(bus: MessageBus) -> None:
    """Phase 2 实现截屏/音频/系统监控。"""


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port)
    build_perception(bus)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
