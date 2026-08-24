import json

import pytest

from yuki.persistence import atomic_write_json


def test_atomic_write_json_roundtrip_creates_parent(tmp_path):
    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1, "b": [2, 3]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_atomic_write_json_overwrites_existing(tmp_path):
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_atomic_write_json_keeps_target_intact_when_rename_crashes(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def boom_replace(*args, **kwargs):
        raise OSError("crash before rename")

    monkeypatch.setattr("yuki.persistence.os.replace", boom_replace)
    with pytest.raises(OSError):
        atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
