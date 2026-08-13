import sys
import time

from yuki.config import Config
from yuki.health import HealthStatus
from yuki.interaction.hotkey import HotkeyManager
from yuki.payloads import ReplyPayload
from yuki.process import ProcessAgent
from yuki.topics import Topics


class TTS:
    """TTS 合成桩：控制台输出。Phase 4 由真实语音合成替换。"""

    def speak(self, text: str) -> None:
        print(f"[yuki] {text}", flush=True)


class FocusManager:
    """打断控制桩：恒可打断。Phase 4 实现抢话检测。"""

    def is_interruptible(self) -> bool:
        return True


class VolumeController:
    """三档位桩：恒 normal。Phase 4 实现安静/普通/活跃切换。"""

    def level(self) -> str:
        return "normal"


class InteractionAgent(ProcessAgent):
    name = "interaction"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 hotkeys=None, tts=None, focus_manager=None, volume_controller=None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._hotkeys = hotkeys or HotkeyManager()
        self._tts = tts or TTS()
        self._focus_manager = focus_manager or FocusManager()
        self._volume_controller = volume_controller or VolumeController()
        self._tts_is_active = False

    def setup(self) -> None:
        def on_reply(topic: str, payload: dict) -> None:
            self._tts.speak(payload["text"])

        def trigger_call() -> None:
            self.bus.publish(Topics.AWAKE, {"source": "hotkey", "ts": time.time()})

        self.bus.subscribe(Topics.REPLY, on_reply)
        self._hotkeys.register("trigger", trigger_call)

        if "--trigger-after" in sys.argv:
            import threading
            delay = float(sys.argv[sys.argv.index("--trigger-after") + 1])

            def delayed() -> None:
                time.sleep(delay)
                self._hotkeys.trigger("trigger")

            threading.Thread(target=delayed, daemon=True).start()

    def teardown(self) -> None:
        pass

    def health_components(self):
        return {
            "tts": lambda: HealthStatus(True, {"output": "console"}),
            "hotkeys": lambda: HealthStatus(True, {"installed": True}),
        }
