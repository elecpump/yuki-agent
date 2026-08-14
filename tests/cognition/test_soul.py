from yuki.cognition.brain.soul import SoulStore


def test_save_then_load_roundtrip(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki", persona_version=1)
    store.save({"proactive_cooldown_s": 137.5})
    loaded = SoulStore(tmp_path / "soul.json", "yuki", 1).load()
    assert loaded == {"proactive_cooldown_s": 137.5}


def test_load_missing_returns_none(tmp_path):
    assert SoulStore(tmp_path / "nope.json", "yuki").load() is None


def test_load_wrong_persona_name_returns_none(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    store.save({"proactive_cooldown_s": 100.0})
    assert SoulStore(tmp_path / "s.json", "aki").load() is None


def test_load_wrong_version_returns_none(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki", persona_version=1)
    store.save({"proactive_cooldown_s": 100.0})
    assert SoulStore(tmp_path / "s.json", "yuki", persona_version=2).load() is None


def test_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SoulStore(path, "yuki").load() is None


def test_save_creates_parent_dirs(tmp_path):
    store = SoulStore(tmp_path / "nested" / "dir" / "soul.json", "yuki")
    store.save({"a": 1})
    assert (tmp_path / "nested" / "dir" / "soul.json").exists()


def test_reset_removes_file(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    store.save({"proactive_cooldown_s": 1.0})
    assert store._path.exists()
    store.reset()
    assert not store._path.exists()
    store.reset()  # 幂等，不抛
