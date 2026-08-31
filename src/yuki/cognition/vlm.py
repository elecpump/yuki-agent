import json
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext

from yuki.cognition.call_tracker import CallTracker
from yuki.cognition.context_cache import ContextCache
from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger
from yuki.model_cache import ModelCacheManager

logger = get_logger("yuki.cognition.vlm")

_PROMPT = (
    "你是阅读助手。请分析这张屏幕截图，输出严格 JSON："
    '{"topic": 主题, "summary": 一两句摘要, "content_type": article|pdf|web|unknown, "key_points": [要点列表]}。'
)

_QUESTION_PROMPT = (
    "你是读屏问答路由助手。请结合用户问题和截图判断截图是否足以回答。"
    "输出严格 JSON："
    '{"topic": 主题, "summary": 一两句摘要, "content_type": article|pdf|web|unknown, '
    '"key_points": [要点列表], "can_answer": true|false}。'
    "只有截图中有足够证据回答用户问题时 can_answer 才为 true。用户问题：{question}"
)


class VisualUnderstander:
    """VLM 读屏 → 阅读情境，带 context cache。"""

    def __init__(
        self,
        model=None,
        processor=None,
        cache: ContextCache | None = None,
        *,
        model_id: str = "Qwen/Qwen3-VL-8B-Instruct",
        cache_dir: str = "",
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] | None = None,
        model_registry: CallTracker | None = None,
        model_name: str = "vlm",
        cache_manager: ModelCacheManager | None = None,
        cache_ttl_s: float | None = None,
    ) -> None:
        self._model = model
        self._processor = processor
        self._cache = cache or ContextCache(cache_manager=cache_manager, ttl_s=cache_ttl_s)
        self._loaded = model is not None and processor is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._model_registry = model_registry
        self._model_name = model_name

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
                from transformers import (
                    AutoProcessor,
                    AutoModelForImageTextToText,
                    BitsAndBytesConfig,
                )
                quant = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype="float16",
                )
                self._model = AutoModelForImageTextToText.from_pretrained(
                    self._model_id,
                    cache_dir=self._cache_dir or None,
                    torch_dtype="auto",
                    device_map="auto",
                    quantization_config=quant,
                )
                self._processor = AutoProcessor.from_pretrained(
                    self._model_id, cache_dir=self._cache_dir or None
                )
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise

    def warmup(self) -> None:
        if self._loaded or not self._gate.can_load():
            return
        def _load_thread():
            try:
                self._load()
            except Exception:
                logger.warning("vlm warmup failed, will degrade to text mode", exc_info=True)
        threading.Thread(target=_load_thread, daemon=True).start()

    def load(self) -> None:
        self._load()

    def unload(self) -> None:
        with self._infer_lock:
            with self._load_lock:
                self._model = None
                self._processor = None
                self._loaded = False
                self._gate.reset()
                self.clear_cache()
        self._empty_torch_cache()

    def reload(self) -> None:
        self.unload()
        self.load()

    def set_model_registry(self, registry: CallTracker | None, model_name: str = "vlm") -> None:
        self._model_registry = registry
        self._model_name = model_name

    def _infer(self, image) -> dict:
        return self._infer_with_prompt(image, _PROMPT, include_can_answer=False)

    def _infer_for_question(self, image, question: str) -> dict:
        return self._infer_with_prompt(
            image,
            _QUESTION_PROMPT.replace("{question}", question or ""),
            include_can_answer=True,
        )

    def _infer_with_prompt(self, image, prompt: str, *, include_can_answer: bool) -> dict:
        self._load()
        messages = [
            {"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ]}
        ]
        text = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._model.device)
        import torch

        with torch.no_grad():
            outputs = self._model.generate(**inputs, max_new_tokens=200)
        generated = outputs[0][inputs["input_ids"].shape[-1]:]
        return self._parse(
            self._processor.decode(generated, skip_special_tokens=True),
            include_can_answer=include_can_answer,
        )

    def _parse(self, raw: str, *, include_can_answer: bool = False) -> dict:
        try:
            data = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
            result = {
                "topic": str(data.get("topic", "")),
                "summary": str(data.get("summary", "")),
                "content_type": str(data.get("content_type", "unknown")),
                "key_points": list(data.get("key_points", [])),
            }
            if include_can_answer:
                result["can_answer"] = bool(data.get("can_answer", False))
            return result
        except (json.JSONDecodeError, AttributeError):
            logger.warning("vlm output parse failed, degrading")
            result = {"topic": "", "summary": "", "content_type": "unknown", "key_points": []}
            if include_can_answer:
                result["can_answer"] = False
            return result

    def understand(self, image, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        if not self._loaded:
            error = self._gate.error_message()
            if error is not None:
                return {"topic": "", "summary": "", "content_type": "unknown",
                        "key_points": [], "degraded": True, "reason": error}
        try:
            with self._model_call_tracker():
                with self._infer_lock:
                    result = self._infer(image)
        except Exception:
            logger.exception("vlm inference failed, degrading")
            result = {"topic": "", "summary": "", "content_type": "unknown",
                      "key_points": [], "degraded": True, "reason": "inference_failed"}
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "")
        if cache_key and not result.get("degraded"):
            self._cache.put(cache_key, result)
        return result

    def understand_for_question(self, image, question: str, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        if not self._loaded:
            error = self._gate.error_message()
            if error is not None:
                return {"topic": "", "summary": "", "content_type": "unknown",
                        "key_points": [], "can_answer": False,
                        "degraded": True, "reason": error}
        try:
            with self._model_call_tracker():
                with self._infer_lock:
                    result = self._infer_for_question(image, question)
        except Exception:
            logger.exception("vlm question inference failed, degrading")
            result = {
                "topic": "",
                "summary": "",
                "content_type": "unknown",
                "key_points": [],
                "can_answer": False,
                "degraded": True,
                "reason": "inference_failed",
            }
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "", include_can_answer=True)
        result["can_answer"] = bool(result.get("can_answer", False))
        if cache_key and not result.get("degraded"):
            self._cache.put(cache_key, result)
        return result

    def clear_cache(self) -> None:
        self._cache.clear()

    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}

    def _empty_torch_cache(self) -> None:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug("torch cuda cache cleanup skipped", exc_info=True)

    def _model_call_tracker(self):
        if self._model_registry is None:
            return nullcontext()
        return self._model_registry.track_call(self._model_name)
