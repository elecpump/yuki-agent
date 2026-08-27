from pathlib import Path

import numpy as np
import pytest

from yuki.config import Config
from yuki.cognition.stt import SpeechRecognizer

MODEL_DIR_CANDIDATES = [
    "",
    r"D:\modelscope\models\iic--SenseVoiceSmall\snapshots\master",
    r"C:\Users\Administrator\.cache\modelscope\models\.model--SenseVoiceSmall\snapshots\master",
    r"D:\huggingface\hub\models--FunAudioLLM--SenseVoiceSmall\snapshots",
]


def _find_model_dir() -> Path | None:
    cfg_dir = Config.from_env().stt.model_dir
    candidates = [cfg_dir] + [c for c in MODEL_DIR_CANDIDATES if c != cfg_dir]
    for cand in candidates:
        path = Path(cand)
        if path.joinpath("model.pt").exists():
            return path
        if any(p.is_dir() and p.joinpath("model.pt").exists() for p in path.glob("*/")):
            return next(p for p in path.glob("*/") if p.joinpath("model.pt").exists())
    return None


def _find_example(model_dir: Path, name: str) -> Path | None:
    for base in (model_dir, model_dir.parent):
        path = base.joinpath("example", name)
        if path.exists():
            return path
    return None


def _load_16k(path: Path) -> np.ndarray:
    sf = pytest.importorskip(
        "soundfile",
        reason="real STT e2e requires the optional soundfile package",
    )

    samples, sr = sf.read(str(path), dtype="float32")
    samples = samples[:, 0] if samples.ndim > 1 else samples
    if sr == 48000:
        samples = samples[::3]
    return samples.astype(np.float32)


@pytest.mark.e2e
def test_real_model_recognizes_example_audio():
    model_dir = _find_model_dir()
    if model_dir is None:
        pytest.skip("local SenseVoice model not found")
    stt = SpeechRecognizer(model_dir=str(model_dir), device="auto")
    for name in ("zh", "en"):
        path = _find_example(model_dir, f"{name}.mp3")
        if path is None:
            continue
        text = stt.recognize(_load_16k(path))
        assert text and text.strip(), f"{name}.mp3 recognized empty text"
