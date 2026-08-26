from pathlib import Path

import numpy as np
import pytest

from yuki.config import TtsConfig
from yuki.interaction.tts import IndexTTSModel, TtsUnavailableError


REQUIRED = (
    "gpt.pth",
    "s2mel.pth",
    "codec.pth",
    "multilingual_zh_ja_yue_char_del.tiktoken",
    "wav2vec2bert_stats.pt",
)

REQUIRED_HF_CACHE = (
    "w2v-bert-2.0",
    "campplus_cn_common.bin",
    "bigvgan",
)


def _config(tmp_path: Path, **overrides) -> TtsConfig:
    model_dir = tmp_path / "checkpoints"
    model_dir.mkdir()
    cfg_path = model_dir / "config.yaml"
    cfg_path.write_text("{}", encoding="utf-8")
    for filename in REQUIRED:
        (model_dir / filename).write_bytes(b"x")
    hf_cache = model_dir / "hf_cache"
    hf_cache.mkdir()
    (hf_cache / "w2v-bert-2.0").mkdir()
    (hf_cache / "campplus_cn_common.bin").write_bytes(b"x")
    (hf_cache / "bigvgan").mkdir()
    reference = tmp_path / "voice.wav"
    reference.write_bytes(b"RIFF")
    values = {
        "enabled": True,
        "cfg_path": str(cfg_path),
        "model_dir": str(model_dir),
        "reference_audio_path": str(reference),
    }
    values.update(overrides)
    return TtsConfig(**values)


class FakeIndexModel:
    def __init__(self):
        self.infer_calls = []
        self.normalize_calls = []

    def normalize_emo_vec(self, vector, apply_bias=False):
        self.normalize_calls.append((vector, apply_bias))
        total = sum(vector)
        scale = min(1.0, 0.8 / total) if total else 1.0
        return [value * scale for value in vector]

    def infer(self, **kwargs):
        self.infer_calls.append(kwargs)
        return iter([np.array([[1.2, 40000.0, -40000.0]], dtype=np.float32)])


def test_model_invocation_normalizes_emotion_and_pcm(tmp_path):
    sdk = FakeIndexModel()
    adapter = IndexTTSModel(_config(tmp_path), model_factory=lambda **kwargs: sdk)
    pcm = list(adapter.synthesize_stream("hello", [1.0, 0, 0, 0, 0, 0, 0, 0]))

    assert sdk.normalize_calls[0][1] is True
    assert sum(sdk.infer_calls[0]["emo_vector"]) <= 0.8
    assert sdk.infer_calls[0]["output_path"] is None
    assert sdk.infer_calls[0]["stream_return"] is True
    np.testing.assert_array_equal(
        np.frombuffer(pcm[0], dtype=np.int16),
        np.array([1, 32767, -32768], dtype=np.int16),
    )


def test_neutral_emotion_is_passed_as_none(tmp_path):
    sdk = FakeIndexModel()
    adapter = IndexTTSModel(_config(tmp_path), model_factory=lambda **kwargs: sdk)
    list(adapter.synthesize_stream("hello", None))
    assert sdk.normalize_calls == []
    assert sdk.infer_calls[0]["emo_vector"] is None


def test_configuration_error_is_permanent_and_degraded(tmp_path):
    calls = []
    config = _config(tmp_path, reference_audio_path=str(tmp_path / "missing.wav"))
    adapter = IndexTTSModel(config, model_factory=lambda **kwargs: calls.append(kwargs))

    with pytest.raises(TtsUnavailableError):
        adapter.synthesize_stream("hello")
    with pytest.raises(TtsUnavailableError):
        adapter.synthesize_stream("again")
    assert calls == []
    assert adapter.config_error is not None
    assert adapter.health()["degraded"] is True


def test_missing_hf_cache_models_is_permanent_config_error(tmp_path):
    config = _config(tmp_path)
    (Path(config.model_dir) / "hf_cache" / "bigvgan").rmdir()
    adapter = IndexTTSModel(config, model_factory=lambda **kwargs: None)

    with pytest.raises(TtsUnavailableError) as exc:
        adapter.synthesize_stream("hello")
    assert "hf_cache" in str(exc.value)
    with pytest.raises(TtsUnavailableError):
        adapter.synthesize_stream("again")
    assert "bigvgan" in adapter.config_error
    assert adapter.health()["degraded"] is True


def test_disabled_model_never_imports_or_loads(tmp_path):
    calls = []
    adapter = IndexTTSModel(TtsConfig(enabled=False), model_factory=lambda **kw: calls.append(kw))
    with pytest.raises(TtsUnavailableError):
        adapter.synthesize_stream("hello")
    assert calls == []
    assert adapter.health()["degraded"] is True
