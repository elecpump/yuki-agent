import time
import threading

from yuki.config import TextConfig
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.text import TextExtractorChain, TextStore


class Provider:
    name = "fake"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract(self, payload, frame=None):
        self.calls.append((payload, frame))
        return self.result

    def health(self):
        return {"ok": True, "source": self.name}


def test_text_store_matches_frame_id_before_current_hwnd():
    store = TextStore()
    first = store.ingest({
        "source": "dom",
        "text": "old frame text",
        "frame_id": 1,
        "hwnd": 100,
        "ts": time.time(),
    })
    store.ingest({
        "source": "dom",
        "text": "current hwnd text",
        "frame_id": 2,
        "hwnd": 100,
        "ts": time.time(),
    })

    assert store.match({"frame_id": 1, "hwnd": 100}, ttl_s=10)["text_id"] == first["text_id"]


def test_text_store_falls_back_when_frame_id_has_no_dom_match():
    store = TextStore()
    dom = store.ingest({
        "source": "dom",
        "text": "dom text from extension",
        "title": "Article",
        "url": "https://example.test/a",
        "ts": time.time(),
    })

    result = store.match(
        {
            "frame_id": 42,
            "hwnd": 100,
            "title": "Article",
            "url": "https://example.test/a",
        },
        ttl_s=10,
    )

    assert result["text_id"] == dom["text_id"]


def test_text_chain_blocks_sensitive_window_before_provider():
    provider = Provider({
        "source": "fake",
        "text": "password: secret",
        "confidence": 1.0,
        "sensitive": False,
        "degraded": False,
        "reason": "",
    })
    chain = TextExtractorChain(
        config=TextConfig(),
        sensitive=SensitiveDetector(class_blacklist={"SecretWindow"}, title_keywords=()),
        providers=[provider],
    )

    result = chain.extract({"class_name": "SecretWindow", "title": "Anything", "hwnd": 1})

    assert result["sensitive"] is True
    assert result["text"] == ""
    assert result["reason"] == "sensitive_window"
    assert provider.calls == []


def test_text_chain_uses_first_available_provider():
    first = Provider(None)
    second = Provider({
        "source": "uia",
        "text": "Article title\n- key point",
        "title": "Article",
        "url": "",
        "confidence": 0.8,
        "sensitive": False,
        "degraded": False,
        "reason": "",
    })
    chain = TextExtractorChain(
        config=TextConfig(),
        sensitive=SensitiveDetector(class_blacklist=set(), title_keywords=()),
        providers=[first, second],
    )

    result = chain.extract({"class_name": "Chrome_WidgetWin_1", "title": "Article"})

    assert result["source"] == "uia"
    assert result["text"].startswith("Article title")
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_text_chain_times_out_slow_provider_and_tries_next():
    release = threading.Event()

    class SlowProvider(Provider):
        name = "slow"

        def extract(self, payload, frame=None):
            self.calls.append((payload, frame))
            release.wait(timeout=2.0)
            return self.result

    slow = SlowProvider({
        "source": "slow",
        "text": "late",
        "confidence": 0.1,
        "sensitive": False,
        "degraded": False,
        "reason": "",
    })
    fast = Provider({
        "source": "fast",
        "text": "fast text",
        "title": "Article",
        "url": "",
        "confidence": 0.9,
        "sensitive": False,
        "degraded": False,
        "reason": "",
    })
    chain = TextExtractorChain(
        config=TextConfig(provider_timeout_ms=20),
        sensitive=SensitiveDetector(class_blacklist=set(), title_keywords=()),
        providers=[slow, fast],
    )

    started = time.monotonic()
    try:
        result = chain.extract({"class_name": "Chrome_WidgetWin_1", "title": "Article"})
    finally:
        release.set()

    assert time.monotonic() - started < 0.5
    assert result["source"] == "fast"
    assert len(slow.calls) == 1
    assert len(fast.calls) == 1


def test_text_store_respects_ttl():
    store = TextStore()
    store.ingest({"source": "dom", "text": "stale", "hwnd": 1, "ts": time.time() - 10})

    assert store.match({"hwnd": 1}, ttl_s=1) is None
