import numpy as np
import pytest

from yuki.cognition.speech_buffer import SpeechBuffer


class FakeVad:
    """is_speech 交替返回以模拟语音/静音。"""

    def __init__(self, pattern):
        self._pattern = list(pattern)

    def is_speech(self, frame, sample_rate):
        return self._pattern.pop(0) if self._pattern else False


def test_silence_only_no_utterance():
    utterances = []
    buf = SpeechBuffer(vad=FakeVad([False] * 30), on_utterance=utterances.append, silent_frames=15)
    for _ in range(20):
        buf.add_frame(np.zeros(320, dtype=np.float32))
    assert utterances == []


def test_speech_then_silence_triggers_utterance():
    utterances = []
    pattern = [True] * 5 + [False] * 20
    buf = SpeechBuffer(vad=FakeVad(pattern), on_utterance=utterances.append, silent_frames=10)
    for _ in pattern:
        buf.add_frame(np.zeros(320, dtype=np.float32))
    assert len(utterances) == 1
    assert utterances[0].shape[0] == 5 * 320  # 语音段整段


def test_reset_clears_accumulation():
    utterances = []
    buf = SpeechBuffer(vad=FakeVad([True] * 5), on_utterance=utterances.append, silent_frames=10)
    for _ in range(5):
        buf.add_frame(np.zeros(320, dtype=np.float32))
    buf.reset()
    buf.add_frame(np.zeros(320, dtype=np.float32))  # 下一帧静音（pattern 耗尽→False）
    assert utterances == []
