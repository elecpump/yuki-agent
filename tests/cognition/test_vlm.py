import pytest
import types

from yuki.cognition.context_cache import ContextCache
from yuki.cognition.vlm import VisualUnderstander

from tests.fakes import RecordingCallTracker


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


def test_load_failure_is_remembered(monkeypatch):
    import sys

    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("missing model")

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander()

    first = vlm.understand(None)
    second = vlm.understand(None)

    assert first["degraded"] is True
    assert second["degraded"] is True
    assert len(calls) == 1


def test_disabled_vlm_never_loads():
    vlm = VisualUnderstander(enabled=False)
    assert vlm._gate.disabled() is True
    assert vlm._gate.can_load() is False
    vlm.warmup()
    assert vlm._loaded is False


def test_understand_recovers_after_retry_window(monkeypatch):
    import sys

    now = [0.0]
    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("missing model")

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander(retry_window_s=10.0, clock=lambda: now[0])
    first = vlm.understand(None)
    second = vlm.understand(None)
    assert first["degraded"] is True
    assert second["reason"] == "model load previously failed"
    assert len(calls) == 1
    now[0] = 10.0
    assert vlm._gate.can_load() is True


def test_understand_does_not_cache_degraded_result():
    vlm = VisualUnderstander(model=object(), processor=object())

    def boom(image):
        raise RuntimeError("oom")

    vlm._infer = boom
    first = vlm.understand(None, cache_key="k1")
    assert first["degraded"] is True
    assert vlm._cache.get("k1") is None


def test_vlm_records_call_tracker_metrics():
    tracker = RecordingCallTracker()
    vlm = VisualUnderstander(model=object(), processor=object(), model_registry=tracker)
    vlm._infer = lambda image: {"topic": "t", "summary": "s", "content_type": "web", "key_points": []}

    assert vlm.understand(None)["topic"] == "t"

    assert tracker.success == 1
    assert tracker.failure == 0


def test_vlm_records_call_tracker_failures():
    tracker = RecordingCallTracker()
    vlm = VisualUnderstander(model=object(), processor=object(), model_registry=tracker)

    def boom(image):
        raise RuntimeError("cuda oom")

    vlm._infer = boom

    assert vlm.understand(None)["degraded"] is True

    assert tracker.success == 0
    assert tracker.failure == 1


def test_load_uses_model_id_cache_dir_and_quant_config(monkeypatch):
    import sys

    calls = {}

    class FakeAuto:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.update({"args": args, "kwargs": kwargs})
            return object()

    class FakeProcessor:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.update({"processor_args": args, "processor_kwargs": kwargs})
            return object()

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAuto,
        AutoProcessor=FakeProcessor,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander(
        model_id="Qwen/Qwen3-VL-8B-Instruct", cache_dir="D:/hf"
    )
    vlm._load()

    assert vlm._loaded is True
    assert calls["args"][0] == "Qwen/Qwen3-VL-8B-Instruct"
    model_kwargs = calls["kwargs"]
    assert model_kwargs["cache_dir"] == "D:/hf"
    assert model_kwargs["quantization_config"] == {"cfg": {"load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": "float16"}}
    assert calls["processor_kwargs"]["cache_dir"] == "D:/hf"


def test_question_prompt_formats_literal_json_without_degrading():
    vlm = VisualUnderstander(model=object(), processor=object())
    prompts = []

    def fake_infer(image, prompt, *, include_can_answer):
        prompts.append(prompt)
        assert include_can_answer is True
        return {"topic": "t", "summary": "s", "content_type": "web", "key_points": [], "can_answer": True}

    vlm._infer_with_prompt = fake_infer

    result = vlm.understand_for_question(None, "这页讲什么")

    assert result["can_answer"] is True
    assert '"topic"' in prompts[0]
    assert "这页讲什么" in prompts[0]
