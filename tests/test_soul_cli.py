import json

from yuki.cognition.brain.soul import SoulStore
from yuki.soul_cli import main


def cli_args(tmp_path, *command):
    return [
        "--config",
        str(tmp_path / "missing.yaml"),
        "--path",
        str(tmp_path / "soul.json"),
        "--snapshots-dir",
        str(tmp_path / "snapshots"),
        "--persona-name",
        "yuki",
        *command,
    ]


def prepared_store(tmp_path):
    store = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
        min_snapshot_interval_s=0,
    )
    store.ensure()
    store.update(traits={"warmth": 0.8}, source="realtime")
    return store


def test_cli_show_current_soul(tmp_path, capsys):
    prepared_store(tmp_path)
    assert main(cli_args(tmp_path, "show")) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1


def test_cli_lists_committed_revisions(tmp_path, capsys):
    prepared_store(tmp_path)
    assert main(cli_args(tmp_path, "list")) == 0
    output = capsys.readouterr().out
    assert "r0" in output
    assert "r1 [current]" in output


def test_cli_list_without_snapshots_has_one_status_line(tmp_path, capsys):
    assert main(cli_args(tmp_path, "list")) == 0
    assert capsys.readouterr().out.splitlines() == [
        "current: r0 (no restorable snapshots)"
    ]


def test_cli_restore_creates_new_revision(tmp_path, capsys):
    prepared_store(tmp_path)
    assert main(cli_args(tmp_path, "restore", "0", "--yes")) == 0
    output = capsys.readouterr().out
    assert "restored r0 as new revision r2" in output
    restored = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
        min_snapshot_interval_s=0,
    ).load_or_default()
    assert restored["revision"] == 2
    assert restored["personality_traits"]["warmth"] == 0.5


def test_cli_unknown_revision_returns_error(tmp_path, capsys):
    prepared_store(tmp_path)
    assert main(cli_args(tmp_path, "restore", "99", "--yes")) == 1
    assert "uncommitted soul revision" in capsys.readouterr().err


def test_cli_restore_can_be_cancelled(tmp_path, capsys, monkeypatch):
    prepared_store(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main(cli_args(tmp_path, "restore", "0")) == 0
    assert "restore cancelled" in capsys.readouterr().out
    assert SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
    ).load_or_default()["revision"] == 1
