import json

import pytest

from yuki.cognition.brain.cooldown import CooldownCalculator
from yuki.cognition.brain.soul import (
    LEGACY_COOLDOWN_KEY,
    SoulStore,
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


def test_legacy_params_shape_migrates_cooldown_state(tmp_path):
    soul_path = tmp_path / "soul.json"
    state_path = tmp_path / "cooldown_state.json"
    soul_path.write_text(
        json.dumps({
            "persona_name": "yuki",
            "persona_version": 1,
            "params": {LEGACY_COOLDOWN_KEY: 240.0},
        }),
        encoding="utf-8",
    )
    loaded = SoulStore(soul_path, "yuki", cooldown_state_path=state_path).load()
    assert loaded["personality_traits"]["warmth"] == 0.5
    assert json.loads(state_path.read_text(encoding="utf-8"))["cooldown_s"] == pytest.approx(240.0)
    assert "persona_version" not in json.loads(soul_path.read_text(encoding="utf-8"))


def test_legacy_soul_does_not_mask_custom_tuner_floor(tmp_path):
    soul_path = tmp_path / "soul.json"
    state_path = tmp_path / "runtime" / "cooldown_state.json"
    legacy_path = tmp_path / "custom" / "old_tuner.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps(
            {
                "persona_name": "yuki",
                "proactive_cooldown_s": 240.0,
                "cooldown_floor_s": 90.0,
            }
        ),
        encoding="utf-8",
    )
    soul_path.write_text(
        json.dumps(
            {
                "persona_name": "yuki",
                "persona_version": 1,
                "params": {LEGACY_COOLDOWN_KEY: 180.0},
            }
        ),
        encoding="utf-8",
    )
    SoulStore(
        soul_path,
        "yuki",
        cooldown_state_path=state_path,
        legacy_tuner_state_path=legacy_path,
    ).load()
    cooldown = CooldownCalculator(path=state_path, legacy_path=legacy_path)
    assert cooldown.cooldown_s == 240.0
    assert cooldown.floor_s == 90.0
