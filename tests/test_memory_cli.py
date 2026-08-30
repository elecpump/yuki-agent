import io
import sqlite3

import pytest

from yuki.memory.cli import main
from yuki.memory.store import MemoryStore


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "mem.db")


def test_add_then_list(db, capsys):
    assert main(["--db", db, "add", "--type", "preference", "--content", "用户喜欢茶",
                 "--source", "user", "--metadata", "topic=茶"]) == 0
    assert main(["--db", db, "list"]) == 0
    out = capsys.readouterr().out
    assert "用户喜欢茶" in out
    assert "preference" in out


def test_query_returns_scored_rows(db, capsys):
    main(["--db", db, "add", "--type", "scenario", "--content", "在读量子计算"])
    main(["--db", db, "add", "--type", "scenario", "--content", "在听音乐"])
    assert main(["--db", db, "query", "计算"]) == 0
    out = capsys.readouterr().out
    assert "在读量子计算" in out
    assert "score=" in out


def test_get_missing_returns_error_code(db):
    assert main(["--db", db, "get", "999"]) == 1


def test_inactive_revisions_require_explicit_admin_flags(db, capsys):
    store = MemoryStore(db)
    memory_id = store.create("preference", "旧偏好版本")
    store.close()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE memories SET state = 'superseded' WHERE id = ?",
            (memory_id,),
        )

    assert main(["--db", db, "list"]) == 0
    assert "旧偏好版本" not in capsys.readouterr().out
    assert main(["--db", db, "list", "--state", "superseded"]) == 0
    assert "state=superseded" in capsys.readouterr().out
    assert main(["--db", db, "get", str(memory_id)]) == 1
    capsys.readouterr()
    assert main(["--db", db, "get", str(memory_id), "--include-inactive"]) == 0
    assert "旧偏好版本" in capsys.readouterr().out


def test_delete_and_strengthen(db):
    assert main(["--db", db, "add", "--type", "personal", "--content", "名字叫小羽"]) == 0
    assert main(["--db", db, "strengthen", "1"]) == 0
    assert main(["--db", db, "delete", "1"]) == 0


def test_delete_missing_returns_error_code(db, capsys):
    assert main(["--db", db, "delete", "999"]) == 1
    assert "memory #999 not found" in capsys.readouterr().err


def test_strengthen_missing_returns_error_code(db, capsys):
    assert main(["--db", db, "strengthen", "999"]) == 1
    assert "memory #999 not found" in capsys.readouterr().err


def test_wipe_requires_confirmation(db, monkeypatch, capsys):
    main(["--db", db, "add", "--type", "preference", "--content", "x"])
    monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))
    assert main(["--db", db, "wipe"]) == 1
    assert MemoryStore(db).all() != []
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    assert main(["--db", db, "wipe"]) == 0
    assert MemoryStore(db).all() == []


def test_wipe_force_skips_prompt(db, monkeypatch, capsys):
    main(["--db", db, "add", "--type", "preference", "--content", "y"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--db", db, "wipe", "--force"]) == 0
    assert MemoryStore(db).all() == []


def test_embeddings_rebuild_indexes_existing_memories(db, capsys):
    assert main(["--db", db, "add", "--type", "preference", "--content", "vector cli memory"]) == 0
    assert main(["--db", db, "--vector-enabled", "embeddings", "rebuild"]) == 0

    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "1"
