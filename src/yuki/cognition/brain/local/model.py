import threading
import time
from collections.abc import Callable, Sequence

from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.local.model")


class LocalChatModel:
    """Lazy local chat model wrapper for router and short local replies."""

    def __init__(
        self,
        model=None,
        tokenizer=None,
        *,
        model_id: str = "Qwen/Qwen3-1.7B-FP8",
        cache_dir: str = "",
        device: str = "auto",
        enabled: bool = True,
        fp8_dequantize: bool = False,
        local_files_only: bool = False,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._device = device
        self._fp8_dequantize = fp8_dequantize
        self._local_files_only = local_files_only
        self._loaded = model is not None and tokenizer is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def warmup(self) -> None:
        if self._loaded or not self._gate.can_load():
            return

        def _load_thread() -> None:
            try:
                self._load()
            except Exception:
                logger.warning("local model warmup failed", exc_info=True)

        threading.Thread(target=_load_thread, daemon=True, name="yuki-local-model-warmup").start()

    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            error = self._gate.error_message()
            if error:
                raise RuntimeError(error)
            try:
                from transformers import AutoModelForCausalLM, AutoTokenizer

                kwargs = {
                    "cache_dir": self._cache_dir or None,
                    "torch_dtype": "auto",
                    "trust_remote_code": True,
                    "local_files_only": self._local_files_only,
                }
                if self._device == "auto":
                    kwargs["device_map"] = "auto"
                if self._fp8_dequantize:
                    from transformers import FineGrainedFP8Config

                    kwargs["quantization_config"] = FineGrainedFP8Config(dequantize=True)
                self._model = AutoModelForCausalLM.from_pretrained(self._model_id, **kwargs)
                self._tokenizer = AutoTokenizer.from_pretrained(
                    self._model_id,
                    cache_dir=self._cache_dir or None,
                    trust_remote_code=True,
                    local_files_only=self._local_files_only,
                )
                generation_config = getattr(self._model, "generation_config", None)
                if generation_config is not None and hasattr(generation_config, "enable_thinking"):
                    generation_config.enable_thinking = False
                if self._device != "auto" and hasattr(self._model, "to"):
                    self._model.to(self._device)
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise

    def generate(
        self,
        messages: Sequence[dict],
        *,
        max_new_tokens: int = 256,
        timeout_ms: int | None = None,
    ) -> str:
        self._load()
        with self._infer_lock:
            prompt = self._format_messages(messages)
            inputs = self._tokenizer(prompt, return_tensors="pt")
            device = getattr(self._model, "device", None)
            if device is not None and hasattr(inputs, "to"):
                inputs = inputs.to(device)
            import torch

            generate_kwargs = {"max_new_tokens": max_new_tokens}
            if timeout_ms is not None:
                generate_kwargs["max_time"] = max(0.001, timeout_ms / 1000.0)
            with torch.no_grad():
                outputs = self._model.generate(**inputs, **generate_kwargs)
            input_len = inputs["input_ids"].shape[-1]
            generated = outputs[0][input_len:]
            return self._tokenizer.decode(generated, skip_special_tokens=True).strip()

    def _format_messages(self, messages: Sequence[dict]) -> str:
        if hasattr(self._tokenizer, "apply_chat_template"):
            return self._tokenizer.apply_chat_template(
                list(messages),
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            parts.append(f"{role}: {content}")
        parts.append("assistant:")
        return "\n".join(parts)

    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}
