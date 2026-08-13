from yuki.cognition.l1 import L1Engine
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.process import ProcessAgent


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._responder = None

    def setup(self) -> None:
        if self._pipeline is None:
            self._pipeline = build_pipeline(
                self.bus,
                vlm=self._vlm,
                sensitive_filter=self._sensitive_filter,
                stt=self._stt,
                frame_client=self._frame_client,
                speech_buffer=self._speech_buffer,
            )
        self._pipeline.warmup_vlm()  # VLM 后台预热（不可用则降级文本模式）
        self._responder = build_l1_responder(self.bus, l1=self._l1 or L1Engine())

    def teardown(self) -> None:
        pass

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "l1": self._health_l1,
            "pipeline": self._health_pipeline,
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(False, {"reason": "no_vlm"})
        return HealthStatus(vlm._loaded, {"loaded": vlm._loaded})

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        return HealthStatus(stt is not None, {"installed": stt is not None})

    def _health_l1(self) -> HealthStatus:
        return HealthStatus(self._responder is not None, {"installed": self._responder is not None})

    def _health_pipeline(self) -> HealthStatus:
        frame_client = getattr(self._pipeline, "_frame_client", None) if self._pipeline else None
        ok = frame_client is not None and hasattr(frame_client, "get_latest")
        return HealthStatus(ok, {"frame_client_available": ok})
