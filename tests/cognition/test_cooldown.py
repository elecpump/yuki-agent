import json

import pytest

from yuki.cognition.brain.cooldown import CooldownCalculator, detect_polarity


def test_activity_bands_and_silence_priority():
    cooldown = CooldownCalculator()
    assert cooldown.base_cooldown(0.0) == 120.0
    for ts in (10.0, 20.0, 30.0):
        cooldown.on_user_utterance("普通消息", ts)
    assert cooldown.base_cooldown(40.0) == 120.0
    cooldown.on_user_utterance("第四条", 40.0)
    assert cooldown.base_cooldown(50.0) == 300.0
    assert cooldown.base_cooldown(641.0) == 60.0


def test_silent_backoff_caps_exponent_and_speak_resets():
    cooldown = CooldownCalculator(max_cooldown_s=600.0)
    delays = []
    for now in (0.0, 1000.0, 2000.0, 3000.0, 4000.0):
        cooldown.on_decision("silent", now)
        delays.append(cooldown.next_available_ts - now)
    assert delays == [120.0, 180.0, 270.0, 405.0, 405.0]
    cooldown.on_decision("speak", 5000.0)
    assert cooldown.silent_streak == 0


def test_feedback_adjusts_effective_delay_without_changing_activity_bands():
    cooldown = CooldownCalculator()
    for ts in (1.0, 2.0, 3.0):
        cooldown.on_user_utterance("别说了", ts)
    assert cooldown.base_cooldown(4.0) == 120.0
    cooldown.on_decision("speak", 4.0)
    assert cooldown.next_available_ts - 4.0 == 405.0
    assert cooldown.base_cooldown(500.0) == 120.0


def test_restored_silent_streak_is_capped(tmp_path):
    path = tmp_path / "cooldown_state.json"
    path.write_text(
        json.dumps(
            {
                "persona_name": "yuki",
                "cooldown_s": 120.0,
                "floor_s": 30.0,
                "silent_streak": 999,
            }
        ),
        encoding="utf-8",
    )
    assert CooldownCalculator(path=path).silent_streak == 3


def test_feedback_floor_persists_and_legacy_state_migrates(tmp_path):
    path = tmp_path / "cooldown_state.json"
    legacy = tmp_path / "tuner_state.json"
    legacy.write_text(
        json.dumps(
            {
                "persona_name": "yuki",
                "proactive_cooldown_s": 240.0,
                "cooldown_floor_s": 60.0,
            }
        ),
        encoding="utf-8",
    )
    cooldown = CooldownCalculator(path=path, legacy_path=legacy)
    assert cooldown.cooldown_s == 240.0
    assert cooldown.floor_s == 60.0
    for ts in (1.0, 2.0, 3.0):
        cooldown.on_user_utterance("别说了", ts)
    restored = CooldownCalculator(path=path)
    assert restored.floor_s == 90.0
    assert restored.cooldown_s == pytest.approx(600.0)
    assert detect_polarity("继续说") == "positive"


def test_persona_mismatch_is_ignored(tmp_path):
    path = tmp_path / "cooldown_state.json"
    path.write_text(
        json.dumps({"persona_name": "other", "cooldown_s": 500.0, "floor_s": 100.0}),
        encoding="utf-8",
    )
    cooldown = CooldownCalculator(path=path, persona_name="yuki")
    assert cooldown.cooldown_s == 120.0
