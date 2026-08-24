import json

import pytest

from yuki.cognition.brain.soul import (
    COOLDOWN_KEY,
    CORE_VALUE_CATALOG,
    PREFS_PER_PERSONA_REGEN,
    SoulStore,
    TunerStateStore,
)


def test_default_soul_shape_has_no_persona_version(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    soul = store.default_soul()
    assert soul["persona_name"] == "yuki"
    assert "persona_version" not in soul
    assert soul["core_values"]
    assert soul["personality_traits"]["warmth"] == 0.5
    assert soul["prefs_since_regen"] == 0


def test_save_then_load_roundtrip_kernel(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    soul = store.default_soul()
    soul["personality_traits"]["warmth"] = 0.7
    soul["prefs_since_regen"] = 2
    store.save(soul)
    loaded = SoulStore(tmp_path / "soul.json", "yuki").load()
    assert loaded["personality_traits"]["warmth"] == pytest.approx(0.7)
    assert loaded["prefs_since_regen"] == 2


def test_soul_save_writes_audit(tmp_path, monkeypatch):
    calls = []

    class FakeAudit:
        def info(self, event, **fields):
            calls.append((event, fields))

    monkeypatch.setattr("yuki.cognition.brain.soul.get_audit_logger", lambda: FakeAudit())
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.save(store.default_soul())
    assert calls[0] == ("soul.save", {"persona": "yuki"})


def test_load_missing_returns_none(tmp_path):
    assert SoulStore(tmp_path / "nope.json", "yuki").load() is None


def test_ensure_creates_default_kernel(tmp_path):
    path = tmp_path / "soul.json"
    soul = SoulStore(path, "yuki").ensure()
    assert path.exists()
    assert soul["persona_name"] == "yuki"


def test_load_wrong_persona_name_returns_none(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    store.save(store.default_soul())
    assert SoulStore(tmp_path / "s.json", "aki").load() is None


def test_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SoulStore(path, "yuki").load() is None


def test_load_non_dict_root_returns_none(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1,2]", encoding="utf-8")
    assert SoulStore(path, "yuki").load() is None


def test_load_bad_kernel_schema_returns_none(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"persona_name": "yuki", "core_values": "x"}', encoding="utf-8")
    assert SoulStore(path, "yuki").load() is None


def test_save_creates_parent_dirs(tmp_path):
    store = SoulStore(tmp_path / "nested" / "dir" / "soul.json", "yuki")
    store.save(store.default_soul())
    assert (tmp_path / "nested" / "dir" / "soul.json").exists()


def test_reset_keeps_default_kernel_file(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    soul = store.default_soul()
    soul["prefs_since_regen"] = 3
    store.save(soul)
    store.reset()
    loaded = store.load()
    assert loaded["prefs_since_regen"] == 0
    assert loaded["core_values"]


def test_legacy_params_shape_migrates_cooldown_to_tuner_state(tmp_path):
    soul_path = tmp_path / "soul.json"
    state_path = tmp_path / "tuner_state.json"
    soul_path.write_text(
        json.dumps({
            "persona_name": "yuki",
            "persona_version": 1,
            "params": {COOLDOWN_KEY: 240.0},
        }),
        encoding="utf-8",
    )
    loaded = SoulStore(soul_path, "yuki", tuner_state_path=state_path).load()
    assert loaded["personality_traits"]["warmth"] == 0.5
    assert TunerStateStore(state_path, "yuki").load()[COOLDOWN_KEY] == pytest.approx(240.0)
    assert "persona_version" not in json.loads(soul_path.read_text(encoding="utf-8"))


def test_tuner_state_roundtrip(tmp_path):
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    state.save({COOLDOWN_KEY: 180.0})
    assert state.load()[COOLDOWN_KEY] == pytest.approx(180.0)


def test_adjust_traits_recenters_before_delta(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    soul = store.default_soul()
    soul["personality_traits"]["warmth"] = 1.0
    store.save(soul)
    traits = store.adjust_traits({"warmth": -0.03, "directness": 0.02})
    assert traits["warmth"] == pytest.approx(0.965)
    assert traits["directness"] == pytest.approx(0.52)


def test_catalogued_preference_promotes_guiding_core_value(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    label = "yuki.rhythm.frequency.low"
    soul = store.on_preference_sedimented(label, 0.8)
    promoted = [v for v in soul["core_values"] if v["id"] == CORE_VALUE_CATALOG[label]["id"]]
    assert promoted
    assert promoted[0]["role"] == "guiding"
    assert soul["prefs_since_regen"] == 1


def test_catalogued_preference_below_confidence_does_not_promote(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    soul = store.on_preference_sedimented("yuki.rhythm.frequency.low", 0.69)
    assert all(v["id"] != "cv.rhythm.restraint" for v in soul["core_values"])


def test_prefs_since_regen_is_persistent_and_resettable(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    for _ in range(PREFS_PER_PERSONA_REGEN):
        store.on_preference_sedimented("yuki.explicit", 1.0)
    assert SoulStore(tmp_path / "soul.json", "yuki").load()["prefs_since_regen"] == 5
    store.reset_prefs_since_regen()
    assert store.load()["prefs_since_regen"] == 0


def test_core_value_feedback_can_modify_guiding_value(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.on_preference_sedimented("yuki.rhythm.frequency.low", 1.0)
    store.apply_core_value_feedback("我不需要主动克制,改成先等我明确邀请")
    value = [v for v in store.load()["core_values"] if v["id"] == "cv.rhythm.restraint"][0]
    assert value["text"] == "先等我明确邀请"
    assert value["source"] == "user"
