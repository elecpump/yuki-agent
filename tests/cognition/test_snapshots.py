import pytest

from yuki.cognition.brain.snapshots import PersonaStore


def make(tmp_path, **kwargs):
    return PersonaStore(tmp_path / "snapshots.json", **kwargs)


def test_save_creates_active_and_increments(tmp_path):
    store = make(tmp_path, max_versions=10)
    s1 = store.save("prompt1", {"cooldown": 120})
    s2 = store.save("prompt2", {"cooldown": 140})
    assert s1.version == 1
    assert s2.version == 2
    assert store.active().version == 2
    assert [v.version for v in store.list_versions()] == [1, 2]


def test_save_skips_identical(tmp_path):
    store = make(tmp_path)
    store.save("same", {"a": 1})
    assert store.save("same", {"a": 1}) is None
    assert len(store.list_versions()) == 1


def test_cap_prunes_oldest_non_locked_keeps_v1(tmp_path):
    store = make(tmp_path, max_versions=3)
    store.save("v1", {})
    store.save("v2", {})
    store.save("v3", {})
    store.lock(2)
    store.save("v4", {})   # 超 cap=3 → 删最旧非锁定（v1 保留，v3 删）
    versions = {v.version for v in store.list_versions()}
    assert 1 in versions
    assert 2 in versions
    assert 4 in versions
    assert 3 not in versions


def test_rollback_and_reset(tmp_path):
    store = make(tmp_path)
    store.save("a", {})
    store.save("b", {})
    store.rollback(1)
    assert store.active().persona_prompt == "a"
    store.reset()
    assert store.active() is None or store.active().version == 1
    assert len(store.list_versions()) <= 1


def test_lock_exempts_from_prune(tmp_path):
    store = make(tmp_path, max_versions=2)
    store.save("v1", {})
    store.save("v2", {})
    store.lock(1)
    store.save("v3", {})   # 超 cap=2 → v1 锁定保留、v2 删
    versions = {v.version for v in store.list_versions()}
    assert versions == {1, 3}


def test_diff_and_export_import(tmp_path):
    store = make(tmp_path)
    store.save("line1\nline2", {})
    store.save("line1\nCHANGED", {})
    diff = store.diff(1, 2)
    assert "CHANGED" in diff
    data = store.export(1)
    store2 = make(tmp_path / "other")
    store2.import_snapshot(data)
    assert store2.active() is None  # 导入不自动设 active
    assert any(v.version == 1 for v in store2.list_versions())


def test_unknown_version_raises(tmp_path):
    store = make(tmp_path)
    with pytest.raises(ValueError):
        store.rollback(99)
    with pytest.raises(ValueError):
        store.lock(99)


def test_diff_unknown_version_raises(tmp_path):
    store = make(tmp_path)
    store.save("a", {})
    store.save("b", {})
    with pytest.raises(ValueError):
        store.diff(1, 99)
    with pytest.raises(ValueError):
        store.diff(99, 1)
    with pytest.raises(ValueError):
        store.diff(99, 100)


def test_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "snapshots.json"
    path.write_text("{broken", encoding="utf-8")
    store = PersonaStore(path)
    assert store.active() is None
    assert store.list_versions() == []


def test_load_wrong_schema_tolerated(tmp_path):
    path = tmp_path / "snapshots.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = PersonaStore(path)
    assert store.active() is None
    assert store.list_versions() == []
    path.write_text('{"versions": [{"version": 1}]}', encoding="utf-8")
    store = PersonaStore(path)
    assert store.active() is None
    assert store.list_versions() == []
