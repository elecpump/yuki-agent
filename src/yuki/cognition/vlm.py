import json
import threading

from yuki.cognition.context_cache import ContextCache
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.vlm")

_PROMPT = (
    "你是阅读助手。请分析这张屏幕截图，输出严格 JSON："
    '{"topic": 主题, "summary": 一两句摘要, "content_type": article|pdf|web|unknown, "key_points": [要点列表]}。'
)


class VisualUnderstander:
    """VLM 读屏 → 阅读情境，带 context cache。"""

    def __init__(self, model=None, processor=None, cache: ContextCache | None = None) -> None:
        self._model = model
        self._processor = processor
        self._cache = cache or ContextCache()
        self._loaded = model is not None and processor is not None
        self._load_failed = False
        self._load_lock = threading.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if self._load_failed:
                raise RuntimeError("vlm load previously failed")
            try:
                from transformers import AutoModel, AutoProcessor
                self._model = AutoModel.from_pretrained(
                    "Qwen/Qwen3-VL-8B", torch_dtype="auto", device_map="auto", load_in_4bit=True
                )
                self._processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-8B")
                self._loaded = True
            except Exception:
                self._load_failed = True
                raise

    def warmup(self) -> None:
        if self._loaded or self._load_failed:
            return
        def _load_thread():
            try:
                self._load()
            except Exception:
                logger.warning("vlm warmup failed, will degrade to text mode", exc_info=True)
        threading.Thread(target=_load_thread, daemon=True).start()

    def _infer(self, image) -> dict:
        self._load()
        from qwen_vl_utils import process_vision_info
        messages = [
            {"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": _PROMPT},
            ]}
        ]
        text = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._model.device)
        import torch

        with torch.no_grad():
            outputs = self._model.generate(**inputs, max_new_tokens=200)
        generated = outputs[0][inputs["input_ids"].shape[-1]:]
        return self._parse(self._processor.decode(generated, skip_special_tokens=True))

    def _parse(self, raw: str) -> dict:
        try:
            data = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
            return {
                "topic": str(data.get("topic", "")),
                "summary": str(data.get("summary", "")),
                "content_type": str(data.get("content_type", "unknown")),
                "key_points": list(data.get("key_points", [])),
            }
        except (json.JSONDecodeError, AttributeError):
            logger.warning("vlm output parse failed, degrading")
            return {"topic": "", "summary": "", "content_type": "unknown", "key_points": []}

    def understand(self, image, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        try:
            result = self._infer(image)
        except Exception:
            logger.exception("vlm inference failed, degrading")
            result = {"topic": "", "summary": "", "content_type": "unknown",
                      "key_points": [], "degraded": True, "reason": "inference_failed"}
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "")
        if cache_key:
            self._cache.put(cache_key, result)
        return result

    def clear_cache(self) -> None:
        self._cache = ContextCache()
