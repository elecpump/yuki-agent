from yuki.logger import get_logger

logger = get_logger("yuki.cognition.l1")

_DEFAULT_RULES = {
    ("你好", "hi", "hello", "在吗"): "你好呀，我在呢。",
    ("谢谢", "感谢"): "不客气～",
    ("嗯", "好的", "继续"): "好，我听着呢。",
}


class L1Engine:
    """L1 本地快答：CPU 常驻规则引擎 + 模型接入接口（<1s）。

    Phase 3 交付规则/模板引擎（零 GPU、零模型依赖）。
    Task 1 基准若证实 Qwen3-0.6B 量化可在 CPU 达标，则接入 generator。
    """

    def __init__(self, generator=None, rules: dict | None = None) -> None:
        self._generator = generator  # 可选：CPU 量化小模型 generator(text) -> str
        self._rules = rules if rules is not None else _DEFAULT_RULES

    def reply(self, text: str, context: dict | None = None) -> str:
        text = (text or "").strip()
        if not text:
            return "我在，你说。"
        if self._generator is not None:
            try:
                return self._generator(text)
            except Exception:
                logger.exception("l1 generator failed")
        lowered = text.lower()
        for keywords, response in self._rules.items():
            if any(kw in lowered for kw in keywords):
                return response
        topic = (context or {}).get("topic")
        if topic:
            return f"嗯，说到{topic}了，你想聊哪方面？"
        return "嗯嗯，我在听。"
