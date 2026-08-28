import json

import pytest

from yuki.cognition.brain.soul import (
    COOLDOWN_KEY,
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
    assert soul["revision"] == 0
    assert "prefs_since_regen" not in soul


def test_save_then_load_roundtrip_kernel(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    soul = store.default_soul()
    soul["personality_traits"]["warmth"] = 0.7
    store.save(soul)
    loaded = SoulStore(tmp_path / "soul.json", "yuki").load()
    assert loaded["personality_traits"]["warmth"] == pytest.approx(0.7)
    assert "prefs_since_regen" not in loaded


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
    soul["personality_traits"]["warmth"] = 0.9
    store.save(soul)
    store.reset()
    loaded = store.load()
    assert loaded["personality_traits"]["warmth"] == pytest.approx(0.5)
    assert loaded["revision"] == 0
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
