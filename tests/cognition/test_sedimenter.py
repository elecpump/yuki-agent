import pytest

from yuki.cognition.brain.sedimenter import (
    LABEL_FREQUENCY_HIGH,
    LABEL_FREQUENCY_LOW,
    PreferenceSedimenter,
)
from yuki.cognition.brain.tuner import FeedbackTuner
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.classifier import Intent
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_sed(tmp_path, **kwargs):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    return PreferenceSedimenter(manager, **kwargs), manager


def labels(memory):
    return [m["metadata"].get("label") for m in memory.list(memory_type="preference")]


def test_rhythm_frequency_sediments_after_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW in labels(memory)


def test_rhythm_not_sedimented_below_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert labels(memory) == []  # 仅 2 次，未达 3


def test_rhythm_frequency_high_sediments(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("说得好", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_HIGH in labels(memory)


def test_correction_resets_sediment_state(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW in labels(memory)
    sed.on_user_utterance("说反了，我其实喜欢主动互动", Intent.SYSTEM)  # 纠正
    assert LABEL_FREQUENCY_LOW not in labels(memory)
    # 纠正后再来 1 次隐式负面，不得借助旧计数重新沉淀
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW not in labels(memory)


def test_persisted_feedback_preference_degradable_after_restart(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "用户不喜欢频繁主动开口", confidence=1.0, source="feedback",
                  metadata={"label": LABEL_FREQUENCY_LOW})
    # 新会话沉淀器从持久化 feedback 行种子恢复 _sedimented
    sed = PreferenceSedimenter(manager, min_signals=3, confidence_threshold=0.6)
    for _ in range(4):
        sed.on_user_utterance("说得好", Intent.CHIT_CHAT)  # 反向信号 → 置信度 0 < 0.6 → 降级删除
    assert LABEL_FREQUENCY_LOW not in labels(manager)


def test_contradicting_signals_lower_confidence(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW in labels(memory)
    # 4 次正向反向 → low 的 contradicts=4 → 置信度 3/(3+4)=0.43 < 0.6 → 降级删除
    for _ in range(4):
        sed.on_user_utterance("说得好", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW not in labels(memory)


def test_length_preference_when_verbose(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3)
    for _ in range(3):
        sed.on_user_utterance("你话太多了，简短点", Intent.CHIT_CHAT)
    assert "yuki.rhythm.length.short" in labels(memory)


def test_explicit_statement_sediments(tmp_path):
    sed, memory = make_sed(tmp_path)
    sed.on_user_utterance("别讲笑话了", Intent.SYSTEM)
    prefs = memory.list(memory_type="preference")
    assert prefs and prefs[0]["source"] == "user"
    assert prefs[0]["confidence"] == 1.0


def test_correction_wipes_implicit_and_pins_explicit(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=1)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)  # 隐式（feedback source）
    assert any(m["source"] == "feedback" for m in memory.list(memory_type="preference"))
    sed.on_user_utterance("其实我不喜欢主动聊天", Intent.SYSTEM)  # 纠正
    prefs = memory.list(memory_type="preference")
    assert not any(m["source"] == "feedback" for m in prefs)
    assert prefs and prefs[0]["source"] == "user"


def test_topic_interest_sediments_after_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, topic_engagement_threshold=3)
    for _ in range(3):
        sed.on_engagement("量子计算")
    assert "yuki.topic.量子计算" in labels(memory)


def test_frequency_preference_sets_tuner_floor(tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = FeedbackTuner(policy, SoulStore(tmp_path / "s.json", "yuki"), cooldown_min_s=30.0)
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    sed = PreferenceSedimenter(memory, tuner=tuner, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert tuner._min_s >= 120.0
    assert tuner.cooldown_s >= 120.0
