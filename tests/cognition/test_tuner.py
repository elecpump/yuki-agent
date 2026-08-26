import pytest

from yuki.cognition.brain import tuner as tuner_mod
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import COOLDOWN_KEY, FLOOR_KEY, TunerStateStore
from yuki.cognition.brain.tuner import FeedbackTuner


def make(policy=None, tmp_path=None, **kwargs):
    policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    return FeedbackTuner(policy, state, **kwargs)


def test_initial_cooldown_from_policy(tmp_path):
    tuner = make(tmp_path=tmp_path)
    assert tuner.cooldown_s == 120.0


def test_window_engagement_warms_up(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 30.0
    tuner.on_user_utterance("嗯嗯")
    assert tuner.cooldown_s == pytest.approx(120.0 * 0.9)
    assert policy.cooldown_s == pytest.approx(120.0 * 0.9)


def test_timeout_after_window_cools_down(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 200.0
    tuner.on_user_utterance("嗯")
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.3)


def test_explicit_negative_strong_cool(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("太吵了，安静点")
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_explicit_negative_applies_outside_window(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("你话太多了")  # 无 _open_ts 也生效
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_explicit_positive_warms(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("说得好，继续")
    assert tuner.cooldown_s == pytest.approx(120.0 * 0.8)


def test_negative_overrides_window_engagement(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 30.0
    tuner.on_user_utterance("太吵了")  # 窗口内但负极性 → 1.5 而非 0.9
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_clamp_lower_and_upper(tmp_path):
    tuner = make(tmp_path=tmp_path, cooldown_min_s=30.0, cooldown_max_s=600.0)
    tuner.adjust(0.0001)
    assert tuner.cooldown_s == 30.0
    tuner.adjust(100000.0)
    assert tuner.cooldown_s == 600.0


def test_adjust_syncs_policy_and_tuner_state(tmp_path):
    policy = DecisionPolicy(120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    tuner = FeedbackTuner(policy, state)
    tuner.adjust(1.5)
    assert policy.cooldown_s == pytest.approx(180.0)
    assert state.load()[COOLDOWN_KEY] == pytest.approx(180.0)


def test_load_soul_restores_cooldown(tmp_path):
    policy = DecisionPolicy(120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    state.save({COOLDOWN_KEY: 240.0})
    tuner = FeedbackTuner(policy, state)
    tuner.load_soul()
    assert tuner.cooldown_s == 240.0
    assert policy.cooldown_s == 240.0


def test_detect_polarity():
    from yuki.cognition.brain.tuner import detect_polarity
    assert detect_polarity("太吵了") == "negative"
    assert detect_polarity("说得好") == "positive"
    assert detect_polarity("随便聊聊") == "neutral"
    assert detect_polarity("") == "neutral"


def test_repeated_negative_feedback_raises_and_persists_floor(tmp_path):
    policy = DecisionPolicy(120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    tuner = FeedbackTuner(policy, state, floor_step_s=30.0, floor_negatives=2)

    tuner.on_user_utterance("太吵了")
    tuner.on_user_utterance("话太多")

    assert state.load()[FLOOR_KEY] == 60.0
    tuner.adjust(0.01)
    assert policy.cooldown_s >= 60.0


def test_load_restores_floor_before_clamping_cooldown(tmp_path):
    policy = DecisionPolicy(120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    state.save({COOLDOWN_KEY: 60.0, FLOOR_KEY: 90.0})
    tuner = FeedbackTuner(policy, state)

    tuner.load_soul()

    assert tuner.cooldown_s == 90.0
    assert policy.cooldown_s == 90.0
    tuner.adjust(0.01)
    assert policy.cooldown_s == 90.0
