from yuki.cognition.l1 import L1Engine
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.process import ProcessAgent


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None,
                 memory: MemoryManager | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._responder = None
        self._memory = memory

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
        if self._memory is None:
            self._memory = MemoryManager(
                MemoryStore(self.config.memory.db_path),
                decay_base=self.config.memory.decay_base,
                decay_lambda=self.config.memory.decay_lambda,
                decay_threshold=self.config.memory.decay_threshold,
            )
        register_memory_services(self.bus, self._memory)

    def teardown(self) -> None:
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "l1": self._health_l1,
            "pipeline": self._health_pipeline,
            "memory": self._health_memory,
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

    def _health_memory(self) -> HealthStatus:
        ok = self._memory is not None and self._memory.ping()
        return HealthStatus(ok, {"db": self.config.memory.db_path})
