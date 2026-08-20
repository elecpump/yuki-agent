from __future__ import annotations

import base64
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Protocol

from yuki.config import TextConfig
from yuki.logger import get_logger
from yuki.perception.capture import FrameStore
from yuki.perception.sensitive import SensitiveDetector

TEXT_SERVICE = "text"
TEXT_INGEST_SERVICE = "text/ingest"

logger = get_logger("yuki.perception.text")


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_text(text: str, max_chars: int) -> str:
    collapsed = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip()[:max_chars]


def _evidence(
    *,
    source: str,
    text: str = "",
    payload: dict | None = None,
    confidence: float = 0.0,
    sensitive: bool = False,
    degraded: bool = False,
    reason: str = "",
    max_chars: int = 50000,
) -> dict:
    payload = payload or {}
    result = {
        "source": source,
        "text": _clean_text(text, max_chars),
        "title": str(payload.get("title", "")),
        "url": str(payload.get("url", "")),
        "confidence": float(confidence),
        "ts": float(payload.get("ts") or time.time()),
        "sensitive": bool(sensitive),
        "degraded": bool(degraded),
        "reason": reason,
    }
    hwnd = _int_or_none(payload.get("hwnd"))
    frame_id = _int_or_none(payload.get("frame_id"))
    if hwnd is not None:
        result["hwnd"] = hwnd
    if frame_id is not None:
        result["frame_id"] = frame_id
    return result


class TextProvider(Protocol):
    name: str

    def extract(self, payload: dict, frame: dict | None = None) -> dict | None: ...

    def health(self) -> dict: ...


class TextStore:
    def __init__(self, *, max_items: int = 128, max_chars: int = 50000) -> None:
        self._max_items = max(1, max_items)
        self._max_chars = max_chars
        self._items: list[dict] = []
        self._next_text_id = 0
        self._lock = threading.Lock()

    def ingest(self, payload: dict) -> dict:
        evidence = _evidence(
            source=str(payload.get("source", "dom")),
            text=str(payload.get("text", "")),
            payload=payload,
            confidence=float(payload.get("confidence", 0.95)),
            sensitive=bool(payload.get("sensitive", False)),
            degraded=bool(payload.get("degraded", False)),
            reason=str(payload.get("reason", "")),
            max_chars=self._max_chars,
        )
        with self._lock:
            self._next_text_id += 1
            evidence["text_id"] = self._next_text_id
            self._items.append(evidence)
            if len(self._items) > self._max_items:
                self._items = self._items[-self._max_items:]
        return dict(evidence)

    def match(self, payload: dict, *, ttl_s: float) -> dict | None:
        now = time.time()
        frame_id = _int_or_none(payload.get("frame_id"))
        hwnd = _int_or_none(payload.get("hwnd"))
        url = str(payload.get("url", ""))
        title = str(payload.get("title", ""))
        with self._lock:
            items = list(reversed(self._items))
        if frame_id is not None:
            for item in items:
                if ttl_s and now - float(item.get("ts", 0.0)) > ttl_s:
                    continue
                if _int_or_none(item.get("frame_id")) == frame_id:
                    return dict(item)
        for item in items:
            if ttl_s and now - float(item.get("ts", 0.0)) > ttl_s:
                continue
            if hwnd is not None and _int_or_none(item.get("hwnd")) == hwnd:
                if not url or not item.get("url") or item.get("url") == url:
                    return dict(item)
            if url and item.get("url") == url:
                return dict(item)
            if title and item.get("title") == title:
                return dict(item)
        return None


class StoredDomTextProvider:
    name = "dom"

    def __init__(self, store: TextStore, *, ttl_s: float) -> None:
        self._store = store
        self._ttl_s = ttl_s

    def extract(self, payload: dict, frame: dict | None = None) -> dict | None:
        hit = self._store.match(payload, ttl_s=self._ttl_s)
        if hit and hit.get("text"):
            return hit
        return None

    def health(self) -> dict:
        return {"ok": True, "source": self.name}


class UiaTextProvider:
    name = "uia"

    def __init__(self, *, max_chars: int = 50000) -> None:
        self._max_chars = max_chars
        self._available = None

    def extract(self, payload: dict, frame: dict | None = None) -> dict | None:
        hwnd = _int_or_none(payload.get("hwnd"))
        if hwnd is None:
            return None
        try:
            import uiautomation as auto

            self._available = True
            control = auto.ControlFromHandle(hwnd)
            text = self._text_from_control(control)
        except Exception:
            self._available = False
            logger.debug("uia text extraction failed", exc_info=True)
            return None
        if not text:
            return None
        return _evidence(
            source=self.name,
            text=text,
            payload=payload,
            confidence=0.75,
            max_chars=self._max_chars,
        )

    def _text_from_control(self, control) -> str:
        chunks = []
        queue = [control]
        while queue and len("\n".join(chunks)) < self._max_chars:
            current = queue.pop(0)
            for attr in ("Name", "Value"):
                value = getattr(current, attr, "")
                if value:
                    chunks.append(str(value))
            try:
                children = current.GetChildren()
            except Exception:
                children = []
            queue.extend(children[:50])
        return "\n".join(dict.fromkeys(c.strip() for c in chunks if c and c.strip()))

    def health(self) -> dict:
        return {"ok": self._available is not False, "source": self.name}


class OcrTextProvider:
    name = "ocr"

    def __init__(
        self,
        frame_store: FrameStore,
        *,
        timeout_ms: int = 250,
        max_chars: int = 50000,
    ) -> None:
        self._frame_store = frame_store
        self.timeout_ms = timeout_ms
        self._timeout_s = timeout_ms / 1000.0
        self._max_chars = max_chars
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yuki-ocr")
        self._ocr = None
        self._available = None

    def extract(self, payload: dict, frame: dict | None = None) -> dict | None:
        frame = frame or self._frame_for_payload(payload)
        if not frame or not frame.get("png") or frame.get("sensitive"):
            return None
        future = self._executor.submit(self._recognize, frame["png"])
        try:
            text = future.result(timeout=self._timeout_s)
        except TimeoutError:
            future.cancel()
            return _evidence(
                source=self.name,
                payload=payload,
                degraded=True,
                reason="ocr_timeout",
                max_chars=self._max_chars,
            )
        except Exception:
            self._available = False
            logger.debug("ocr text extraction failed", exc_info=True)
            return None
        if not text:
            return None
        return _evidence(
            source=self.name,
            text=text,
            payload=payload,
            confidence=0.55,
            max_chars=self._max_chars,
        )

    def _frame_for_payload(self, payload: dict) -> dict:
        return self._frame_store.latest()

    def _recognize(self, png_b64: str) -> str:
        if self._ocr is None:
            from paddleocr import PaddleOCR

            try:
                self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            except TypeError:
                self._ocr = PaddleOCR(lang="ch")
            self._available = True
        raw = base64.b64decode(png_b64)
        import numpy as np
        from PIL import Image

        image = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
        result = self._ocr.ocr(image, cls=True)
        lines = []
        for page in result or []:
            for item in page or []:
                if len(item) >= 2 and item[1]:
                    lines.append(str(item[1][0]))
        return "\n".join(lines)

    def health(self) -> dict:
        return {"ok": self._available is not False, "source": self.name}


class TextExtractorChain:
    def __init__(
        self,
        *,
        config: TextConfig,
        sensitive: SensitiveDetector,
        providers: list[TextProvider],
        class_name_for_hwnd=None,
        title_for_hwnd=None,
    ) -> None:
        self._config = config
        self._sensitive = sensitive
        self._providers = providers
        self._class_name_for_hwnd = class_name_for_hwnd
        self._title_for_hwnd = title_for_hwnd
        self._last_provider = ""
        self._provider_timeout_s = config.provider_timeout_ms / 1000.0
        self._provider_executor = ThreadPoolExecutor(
            max_workers=max(1, len(providers)),
            thread_name_prefix="yuki-text-provider",
        )
        self._provider_timeouts: dict[str, int] = {}

    def extract(self, payload: dict, frame: dict | None = None) -> dict:
        payload = dict(payload or {})
        if frame:
            payload.setdefault("frame_id", frame.get("frame_id"))
            payload.setdefault("hwnd", frame.get("hwnd"))
        class_name, title = self._window_identity(payload)
        if self._sensitive.is_sensitive(class_name, title):
            return _evidence(
                source="blocked",
                payload=payload,
                sensitive=True,
                degraded=True,
                reason="sensitive_window",
                max_chars=self._config.max_chars,
            )
        for provider in self._providers:
            result = self._extract_with_timeout(provider, payload, frame)
            if result is None:
                continue
            if result.get("text") or result.get("sensitive") or result.get("degraded"):
                self._last_provider = provider.name
                return result
        return _evidence(
            source="none",
            payload=payload,
            degraded=True,
            reason="no_text",
            max_chars=self._config.max_chars,
        )

    def _extract_with_timeout(
        self,
        provider: TextProvider,
        payload: dict,
        frame: dict | None,
    ) -> dict | None:
        timeout_s = getattr(provider, "timeout_ms", None)
        timeout_s = (
            float(timeout_s) / 1000.0
            if timeout_s is not None
            else self._provider_timeout_s
        )
        future = self._provider_executor.submit(provider.extract, payload, frame)
        try:
            return future.result(timeout=timeout_s)
        except TimeoutError:
            future.cancel()
            self._provider_timeouts[provider.name] = (
                self._provider_timeouts.get(provider.name, 0) + 1
            )
            logger.warning("text provider timed out", provider=provider.name)
            return None

    def _window_identity(self, payload: dict) -> tuple[str, str]:
        class_name = str(payload.get("class_name", "") or "")
        title = str(payload.get("title", "") or "")
        hwnd = _int_or_none(payload.get("hwnd"))
        if hwnd is not None:
            if not class_name:
                class_name = self._class_name(hwnd)
            if not title:
                title = self._title(hwnd)
        return class_name, title

    def _class_name(self, hwnd: int) -> str:
        if self._class_name_for_hwnd is not None:
            return self._class_name_for_hwnd(hwnd) or ""
        try:
            import win32gui

            return win32gui.GetClassName(hwnd)
        except Exception:
            return ""

    def _title(self, hwnd: int) -> str:
        if self._title_for_hwnd is not None:
            return self._title_for_hwnd(hwnd) or ""
        try:
            import win32gui

            return win32gui.GetWindowText(hwnd)
        except Exception:
            return ""

    def health(self) -> dict:
        return {
            "ok": True,
            "providers": [provider.health() for provider in self._providers],
            "last_provider": self._last_provider,
            "provider_timeouts": dict(self._provider_timeouts),
        }


def build_text_services(
    bus,
    *,
    config: TextConfig,
    sensitive: SensitiveDetector,
    frame_store: FrameStore,
) -> TextExtractorChain:
    store = TextStore(max_chars=config.max_chars)
    providers: list[TextProvider] = []
    if config.dom_enabled:
        providers.append(StoredDomTextProvider(store, ttl_s=config.ttl_s))
    if config.uia_enabled:
        providers.append(UiaTextProvider(max_chars=config.max_chars))
    if config.ocr_enabled:
        providers.append(
            OcrTextProvider(
                frame_store,
                timeout_ms=config.ocr_timeout_ms,
                max_chars=config.max_chars,
            )
        )
    chain = TextExtractorChain(config=config, sensitive=sensitive, providers=providers)

    bus.respond(TEXT_INGEST_SERVICE, lambda payload: store.ingest(payload))
    bus.respond(TEXT_SERVICE, lambda payload: chain.extract(payload))
    return chain
