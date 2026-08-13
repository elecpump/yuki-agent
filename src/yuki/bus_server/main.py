import time

from yuki.bus import BusHub
from yuki.config import Config
from yuki.shutdown import ShutdownManager


def main() -> None:
    config = Config.from_env()
    bus = BusHub(base_port=config.base_port, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
