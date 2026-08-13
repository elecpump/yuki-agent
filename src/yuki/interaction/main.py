import sys
import threading
import time

from yuki.bus import BusNode
from yuki.config import Config
from yuki.health import register_health_service
from yuki.interaction.hotkey import HotkeyManager
from yuki.shutdown import ShutdownManager
from yuki.topics import Topics


def build_interaction(bus: BusNode, hotkeys: HotkeyManager) -> None:
    def on_reply(topic: str, payload: dict) -> None:
        print(f"[yuki] {payload['text']}", flush=True)

    def trigger_call() -> None:
        bus.publish(Topics.AWAKE, {"source": "hotkey", "ts": time.time()})

    bus.subscribe(Topics.REPLY, on_reply)
    hotkeys.register("trigger", trigger_call)


def main() -> None:
    config = Config.from_env()
    bus = BusNode(base_port=config.base_port, hwm=config.hwm)
    hotkeys = HotkeyManager()
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_interaction(bus, hotkeys)
    register_health_service(bus, "interaction")

    if "--trigger-after" in sys.argv:
        delay = float(sys.argv[sys.argv.index("--trigger-after") + 1])

        def delayed() -> None:
            time.sleep(delay)
            hotkeys.trigger("trigger")

        threading.Thread(target=delayed, daemon=True).start()

    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
