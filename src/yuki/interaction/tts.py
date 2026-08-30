import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import yaml

from yuki.cognition.load_gate import LoadGate
from yuki.config import TtsConfig
from yuki.logger import get_logger


logger = get_logger("yuki.interaction.tts")

_REQUIRED_CHECKPOINTS = (
    "gpt.pth",
    "s2mel.pth",
    "codec.pth",
    "multilingual_zh_ja_yue_char_del.tiktoken",
    "wav2vec2bert_stats.pt",
)
# SDK 构造器硬编码的辅助模型路径（`infer_v2_5.py` 内直接拼 `model_dir/hf_cache/...`，
# config.yaml 不引用，`_referenced_local_files` 扫不到）：缺失 = 永久配置错误。
# 注意：SDK 缺失时会尝试 `ensure_models_available` 自动下载；这里选择确定性校验，
# 首次部署需手动跑一次 SDK 或手工放置这些文件。
_REQUIRED_HF_CACHE = (
    "w2v-bert-2.0",
    "campplus_cn_common.bin",
    "bigvgan",
)
_LOCAL_MODEL_SUFFIXES = {".bin", ".json", ".model", ".pth", ".pt", ".safetensors", ".tiktoken"}


class TtsUnavailableError(RuntimeError):
    pass


class IndexTTSModel:
    """Lazy IndexTTS 2.5 adapter with retryable runtime failures."""

    def __init__(
        self,
        config: TtsConfig,
        *,
        model_factory: Callable[..., object] | None = None,
        gate: LoadGate | None = None,
    ) -> None:
        self._config = config
        self._model_factory = model_factory
        self._gate = gate or LoadGate(
            enabled=config.enabled,
            retry_window_s=config.retry_window_s,
        )
        self._load_lock = threading.Lock()
        self._model = None
        self._config_error: str | None = None

    @property
    def config_error(self) -> str | None:
        return self._config_error

    def _referenced_local_files(self, cfg_path: Path) -> list[Path]:
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read TTS config {cfg_path}: {exc}") from exc

        values: list[str] = []

        def visit(value) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
            elif isinstance(value, str):
                normalized = value.lower()
                if "://" not in normalized and Path(value).suffix.lower() in _LOCAL_MODEL_SUFFIXES:
                    values.append(value)

        visit(raw)
        paths: list[Path] = []
        for value in values:
            candidate = Path(value)
            if candidate.is_absolute():
                paths.append(candidate)
                continue
            model_candidate = Path(self._config.model_dir) / candidate
            cfg_candidate = cfg_path.parent / candidate
            paths.append(model_candidate if model_candidate.exists() else cfg_candidate)
        return paths

    def _validate_config(self) -> None:
        cfg_path = Path(self._config.cfg_path)
        model_dir = Path(self._config.model_dir)
        reference = Path(self._config.reference_audio_path)
        errors: list[str] = []
        if not cfg_path.is_file():
            errors.append(f"cfg_path is not a file: {cfg_path}")
        if not model_dir.is_dir():
            errors.append(f"model_dir is not a directory: {model_dir}")
        if not reference.is_file() or not os.access(reference, os.R_OK):
            errors.append(f"reference audio is missing or unreadable: {reference}")
        if model_dir.is_dir():
            for filename in _REQUIRED_CHECKPOINTS:
                if not (model_dir / filename).is_file():
                    errors.append(f"required checkpoint is missing: {model_dir / filename}")
            for entry in _REQUIRED_HF_CACHE:
                if not (model_dir / "hf_cache" / entry).exists():
                    errors.append(f"required hf_cache model is missing: {model_dir / 'hf_cache' / entry}")
        if cfg_path.is_file():
            try:
                referenced = self._referenced_local_files(cfg_path)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                for path in referenced:
                    if not path.exists():
                        errors.append(f"referenced model file is missing: {path}")
        if errors:
            raise ValueError("; ".join(errors))

    def _create_model(self):
        factory = self._model_factory
        if factory is None:
            # 懒导入：默认环境不装 indextts 也能跑（enabled=False 路径）。
            # 副作用注意：import 该模块会设置 os.environ["HF_HUB_CACHE"]（SDK 模块级行为），
            # 且推理时 SDK 会向 stdout 打印进度（不影响 [yuki] 前缀断言）。
            # 该副作用会把后续 HF 模型加载（VLM/embedding）的缓存目录重定向到
            # checkpoints/hf_cache：huggingface_hub 在导入时会把缓存目录固化为
            # constants.HF_HUB_CACHE，仅恢复环境变量不足以解毒，因此导入前先
            # 捕获常量、导入后连同环境变量一起恢复。
            try:
                import huggingface_hub.constants as hf_constants

                saved_hf_cache = hf_constants.HF_HUB_CACHE
            except Exception:
                hf_constants = None
                saved_hf_cache = None
            saved_env = os.environ.get("HF_HUB_CACHE")
            try:
                from indextts.infer_v2_5 import IndexTTS2
            finally:
                if saved_env is None:
                    os.environ.pop("HF_HUB_CACHE", None)
                else:
                    os.environ["HF_HUB_CACHE"] = saved_env
                if hf_constants is not None and saved_hf_cache is not None:
                    hf_constants.HF_HUB_CACHE = saved_hf_cache

            factory = IndexTTS2
        return factory(
            cfg_path=self._config.cfg_path,
            model_dir=self._config.model_dir,
            use_bf16=self._config.use_bf16,
        )

    def _load(self):
        if self._config_error is not None:
            raise TtsUnavailableError(self._config_error)
        if self._model is not None:
            return self._model
        if not self._gate.can_load():
            raise TtsUnavailableError(self._gate.error_message() or "TTS model unavailable")

        with self._load_lock:
            if self._config_error is not None:
                raise TtsUnavailableError(self._config_error)
            if self._model is not None:
                return self._model
            if not self._gate.can_load():
                raise TtsUnavailableError(self._gate.error_message() or "TTS model unavailable")
            try:
                self._validate_config()
            except ValueError as exc:
                self._config_error = str(exc)
                logger.warning("IndexTTS configuration invalid", error=self._config_error)
                raise TtsUnavailableError(self._config_error) from exc
            try:
                self._model = self._create_model()
            except Exception as exc:
                self._gate.mark_failure()
                logger.warning("IndexTTS model load failed", error=str(exc))
                raise TtsUnavailableError("IndexTTS model load failed") from exc
            self._gate.mark_success()
            return self._model

    def warmup(self) -> None:
        if not self._gate.can_load() or self._config_error is not None or self._model is not None:
            return

        def run() -> None:
            try:
                self._load()
            except TtsUnavailableError:
                pass

        threading.Thread(target=run, daemon=True, name="yuki-tts-warmup").start()

    def load(self) -> None:
        self._load()

    def unload(self) -> None:
        with self._load_lock:
            self._model = None
            self._gate.reset()

    @staticmethod
    def _tensor_to_pcm(chunk) -> bytes:
        value = chunk
        for method in ("detach", "cpu", "contiguous"):
            operation = getattr(value, method, None)
            if callable(operation):
                value = operation()
        to_numpy = getattr(value, "numpy", None)
        if callable(to_numpy):
            value = to_numpy()
        samples = np.asarray(value)
        if samples.size == 0:
            return b""
        non_singleton = [dimension for dimension in samples.shape if dimension != 1]
        if samples.ndim > 1 and len(non_singleton) > 1:
            raise ValueError(f"IndexTTS returned non-mono audio shape {samples.shape}")
        samples = np.clip(samples.reshape(-1), -32768, 32767).astype(np.int16, copy=False)
        return samples.tobytes()

    def synthesize_stream(
        self,
        text: str,
        emotion_vector: list[float] | None = None,
        ref_audio: str | None = None,
        lang: str | None = None,
    ) -> Iterator[bytes]:
        model = self._load()
        normalized = None
        if emotion_vector is not None:
            normalized = model.normalize_emo_vec(emotion_vector, apply_bias=True)
        chunks = model.infer(
            spk_audio_prompt=ref_audio or self._config.reference_audio_path,
            text=text,
            output_path=None,
            lang=lang or self._config.language,
            emo_vector=normalized,
            stream_return=True,
        )

        def generate() -> Iterator[bytes]:
            for chunk in chunks:
                pcm = self._tensor_to_pcm(chunk)
                if pcm:
                    yield pcm

        return generate()

    def health(self) -> dict:
        gate_health = self._gate.health()
        return {
            "loaded": self._model is not None,
            **gate_health,
            "config_error": self._config_error,
            "degraded": bool(self._config_error) or bool(gate_health["degraded"]),
        }
