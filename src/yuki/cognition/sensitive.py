import re

_DEFAULT_PATTERNS = {
    "id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "bank_card": r"(?<!\d)\d{16,19}(?!\d)",
    "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
    "password": r"(密码|口令|password|passwd)\s*[:：]\s*\S+",
    "secret": r"(secret|api_key|token)(?![\w])|凭据",
}


class SensitiveFilter:
    """文本级敏感过滤（第二道防线）：VLM 产出的情境进 L1/L2 前检查。

    与采集层窗口级 SensitiveDetector 配合：窗口级阻断截图源头，
    这里是文本级拦截识别结果中的敏感信息。
    """

    def __init__(self, patterns: dict[str, str] | tuple[str, ...] | None = None) -> None:
        if patterns is None:
            source = _DEFAULT_PATTERNS
        elif isinstance(patterns, dict):
            source = patterns
        else:
            source = {f"custom{i}": pat for i, pat in enumerate(patterns)}
        self._patterns = source
        self._compiled = {name: re.compile(pat) for name, pat in self._patterns.items()}

    def scan(self, text: str) -> list[str]:
        text = text or ""
        hits = [name for name, rx in self._compiled.items() if rx.search(text)]
        return hits

    def is_sensitive(self, text: str) -> bool:
        return bool(self.scan(text))
