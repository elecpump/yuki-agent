import pytest

from yuki.cognition.context_cache import ContextCache
from yuki.cognition.vlm import VisualUnderstander


def test_context_cache_hit_and_miss():
    cache = ContextCache(max_entries=2)
    assert cache.get("a") is None
    cache.put("a", {"topic": "x"})
    assert cache.get("a") == {"topic": "x"}


def test_context_cache_lru_eviction():
    cache = ContextCache(max_entries=2)
    cache.put("a", {"n": 1})
    cache.put("b", {"n": 2})
    cache.get("a")  # a 最近使用
    cache.put("c", {"n": 3})  # b 被淘汰
    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_understand_uses_cache():
    calls = []

    class FakeModel:
        def generate(self, *a, **kw):
            calls.append(1)
            return "noop"

    class FakeProcessor:
        def apply_chat_template(self, messages, tokenize=False, **kw):
            return "template"

    vlm = VisualUnderstander(model=FakeModel(), processor=FakeProcessor())
    # fake: understand 直接返回固定 dict（不依赖真实推理）
    vlm._infer = lambda image: {"topic": "t", "summary": "s", "content_type": "article", "key_points": ["a"]}

    first = vlm.understand(None, cache_key="k1")
    assert first["topic"] == "t"
    second = vlm.understand(None, cache_key="k1")
    assert second is first  # 缓存命中，同对象


def test_understand_parse_failure_degrades():
    vlm = VisualUnderstander(model=object(), processor=object())
    vlm._infer = lambda image: "not json"
    result = vlm.understand(None)
    assert result["topic"] == ""
    assert result["content_type"] == "unknown"


def test_understand_inference_failure_degrades():
    class BoomModel:
        pass

    vlm = VisualUnderstander(model=BoomModel(), processor=object())

    def boom(image):
        raise RuntimeError("oom")

    vlm._infer = boom
    result = vlm.understand(None)
    assert result["degraded"] is True
    assert result["reason"] == "inference_failed"
    assert result["topic"] == ""


def test_warmup_is_idempotent_and_background():
    import time

    vlm = VisualUnderstander(model=None, processor=None)
    vlm._load = lambda: setattr(vlm, "_loaded", True)
    vlm.warmup()
    vlm.warmup()  # 幂等
    deadline = time.time() + 2.0
    while not vlm._loaded and time.time() < deadline:
        time.sleep(0.01)
    assert vlm._loaded
