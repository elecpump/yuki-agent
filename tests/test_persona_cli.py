import json

from yuki.cognition.brain.snapshots import PersonaStore
from yuki.persona_cli import main


def test_cli_list_and_active(tmp_path, capsys):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("你好呀", {"cooldown": 120})
    assert main(["--path", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "v1" in out
    assert main(["--path", str(db), "active"]) == 0
    assert "你好呀" in capsys.readouterr().out


def test_cli_rollback_lock_reset(tmp_path):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("a", {})
    store.save("b", {})
    assert main(["--path", str(db), "rollback", "1"]) == 0
    assert PersonaStore(db).active().persona_prompt == "a"
    assert main(["--path", str(db), "lock", "1"]) == 0
    assert main(["--path", str(db), "reset"]) == 0
    assert len(PersonaStore(db).list_versions()) <= 1


def test_cli_diff_and_export_import(tmp_path, capsys):
    db = tmp_path / "snap.json"
    store = PersonaStore(db)
    store.save("line1\nline2", {})
    store.save("line1\nCHANGED", {})
    assert main(["--path", str(db), "diff", "1", "2"]) == 0
    assert "CHANGED" in capsys.readouterr().out
    assert main(["--path", str(db), "export", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    imported = tmp_path / "imp.json"
    (tmp_path / "dump.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert main(["--path", str(imported), "import", str(tmp_path / "dump.json")]) == 0
    assert any(v.version == 1 for v in PersonaStore(imported).list_versions())


def test_cli_unknown_version_errors(tmp_path):
    db = tmp_path / "snap.json"
    PersonaStore(db).save("x", {})
    assert main(["--path", str(db), "rollback", "99"]) == 1
