import types

import pytest

from yuki.cognition.brain.local.model import LocalChatModel


def test_disabled_model_never_loads():
    model = LocalChatModel(enabled=False)
    assert model._gate.disabled() is True
    model.warmup()
    assert model._loaded is False


def test_load_failure_blocks_until_window(monkeypatch):
    import sys

    now = [0.0]
    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("missing model")

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("missing tokenizer")

    fake_transformers = types.SimpleNamespace(
        AutoModelForCausalLM=FakeAutoModel,
        AutoTokenizer=FakeTokenizer,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model = LocalChatModel(retry_window_s=10.0, clock=lambda: now[0])
    with pytest.raises(RuntimeError, match="missing model"):
        model._load()
    with pytest.raises(RuntimeError, match="load previously failed"):
        model._load()
    assert len(calls) == 1
    now[0] = 10.0
    assert model._gate.can_load() is True


def test_health_reports_disabled_as_degraded():
    model = LocalChatModel(enabled=False)
    health = model.health()
    assert health["loaded"] is False
    assert health["enabled"] is False
    assert health["degraded"] is True


def test_unload_clears_loaded_model_and_allows_retry():
    model = LocalChatModel(model=object(), tokenizer=object())
    model._gate.mark_failure()

    model.unload()

    assert model._loaded is False
    assert model._model is None
    assert model._tokenizer is None
    assert model._gate.can_load() is True
