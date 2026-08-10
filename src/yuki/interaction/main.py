import sys
import threading
import time

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.interaction.hotkey import HotkeyManager
from yuki.topics import Topics


def build_interaction(bus: MessageBus, hotkeys: HotkeyManager) -> None:
    def on_reply(topic: str, payload: dict) -> None:
        print(f"[yuki] {payload['text']}", flush=True)

    def trigger_call() -> None:
        bus.publish(Topics.AWAKE, {"source": "hotkey", "ts": time.time()})

    bus.subscribe(Topics.REPLY, on_reply)
    hotkeys.register("trigger", trigger_call)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role="node")
    hotkeys = HotkeyManager()
    build_interaction(bus, hotkeys)

    if "--trigger-after" in sys.argv:
        delay = float(sys.argv[sys.argv.index("--trigger-after") + 1])

        def delayed() -> None:
            time.sleep(delay)
            hotkeys.trigger("trigger")

        threading.Thread(target=delayed, daemon=True).start()

    hotkeys.run()


if __name__ == "__main__":
    main()
