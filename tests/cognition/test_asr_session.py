import numpy as np

from yuki.cognition.asr_session import AsrSession


class FakeSpeechBuffer:
    def __init__(self):
        self.frames = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        self.frames = []

    def add_frame(self, samples):
        self.frames.append(samples)


def _session(now, sb=None, **kw):
    return AsrSession(
        listen_timeout_s=1.0,
        listen_window_s=0.5,
        pre_roll_s=0.04,
        audio_frame_ms=20,
        clock=lambda: now[0],
        speech_buffer=sb or FakeSpeechBuffer(),
        **kw,
    )


def test_begin_starts_listening_and_returns_pre_roll():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    assert session.begin() == []
    assert session.state == "listening"
    assert session.session_id is not None


def test_begin_ignored_when_not_idle():
    now = [10.0]
    session = _session(now)
    first = session.begin()
    second = session.begin()  # 已在 listening
    assert second == []
    assert session.state == "listening"


def test_feed_before_begin_returns_false():
    now = [10.0]
    session = _session(now)
    assert session.feed(np.zeros(320, dtype=np.float32)) is False
    assert session.state == "idle"


def test_feed_after_begin_buffers_and_returns_true():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    session.begin()
    assert session.feed(np.zeros(320, dtype=np.float32)) is True
    assert len(sb.frames) == 1


def test_check_due_returns_to_idle_after_timeout():
    now = [10.0]
    session = _session(now)
    session.begin()
    now[0] += 1.1
    assert session.check_due() is True
    assert session.state == "idle"


def test_consume_utterance_marks_processing():
    now = [10.0]
    session = _session(now)
    session.begin()
    sid = session.session_id
    assert session.consume_utterance(sid) is True
    assert session.state == "processing"
    # 过期 session 拒绝
    assert session.consume_utterance(sid + 999) is False


def test_is_current_matches_session():
    now = [10.0]
    session = _session(now)
    session.begin()
    sid = session.session_id
    assert session.is_current(sid) is True
    assert session.is_current(sid + 1) is False


def test_tts_state_discards_audio_and_clears_pre_roll():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    before = np.ones(320, dtype=np.float32)
    session.feed(before)

    session.enter_tts()
    assert session.state == "tts"
    assert session.session_id is None
    assert session.feed(np.ones(320, dtype=np.float32) * 2) is False
    assert sb.frames == []

    session.exit_tts()
    assert session.state == "idle"
    assert session.begin() == []


def test_tts_transitions_are_idempotent_and_cancel_processing():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    session.begin()
    sid = session.session_id
    assert session.consume_utterance(sid) is True

    session.enter_tts()
    resets_after_enter = sb.reset_calls
    session.enter_tts()
    assert session.state == "tts"
    assert sb.reset_calls == resets_after_enter
    assert session.is_current(sid) is False

    session.exit_tts()
    resets_after_exit = sb.reset_calls
    session.exit_tts()
    assert session.state == "idle"
    assert sb.reset_calls == resets_after_exit
