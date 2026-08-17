import json
import sys
import time
from pathlib import Path

from yuki.bus import BusError, BusTimeoutError
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE
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
    """三档位：quiet / normal / active。档位持久化到本地，进程重启后恢复（§8.1）。

    Phase 4 接入真实系统音量控制，切换走 set_level。
    """

    LEVELS = ("quiet", "normal", "active")

    def __init__(self, path: str | Path = "data/volume_tier.json") -> None:
        self._path = Path(path)
        self._level = self._restore()

    def _restore(self) -> str:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("level") in self.LEVELS:
                return data["level"]
        except (OSError, ValueError):
            pass
        return "normal"

    def level(self) -> str:
        return self._level

    def set_level(self, level: str) -> None:
        if level not in self.LEVELS:
            raise ValueError(f"unknown volume level: {level!r}")
        self._level = level
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"level": level}, ensure_ascii=False), encoding="utf-8"
        )


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
            try:
                reply = self.bus.request(
                    COGNITION_AWAKE_SERVICE,
                    {"source": "hotkey", "ts": time.time()},
                    timeout_ms=self.config.health.timeout_ms,
                )
            except (BusError, BusTimeoutError):
                self._tts.speak("我现在连接不上 cognition。")
                return
            text = (reply or {}).get("text", "")
            if text:
                self._tts.speak(text)

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
            "hotkeys": lambda: HealthStatus(
                "trigger" in getattr(self._hotkeys, "_handlers", {}),
                {"installed": "trigger" in getattr(self._hotkeys, "_handlers", {})},
            ),
        }
