from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.memory.store")

MEMORY_TYPES = ("preference", "personal", "scenario", "reflection")


class MemoryError(Exception):
    """记忆存储错误。"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id            INTEGER PRIMARY KEY,
            memory_type   TEXT NOT NULL CHECK (memory_type IN ('preference','personal','scenario','reflection')),
            content       TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 0.5,
            sensitivity   INTEGER NOT NULL DEFAULT 0 CHECK (sensitivity IN (0,1,2)),
            source        TEXT NOT NULL DEFAULT 'cli',
            metadata      TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            last_access   REAL NOT NULL,
            access_count  INTEGER NOT NULL DEFAULT 0,
            strengthened  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
        "content, content='memories', content_rowid='id', tokenize='trigram')"
    )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )


def _uses_fts(text: str) -> bool:
    """最短分词 >=3 字符才用 FTS；否则 trigram 无法匹配，走 LIKE 兜底。"""
    return min((len(tok) for tok in text.split()), default=0) >= 3


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


class MemoryStore:
    """SQLite 持久化 + FTS5 trigram 检索 + <3 字符 LIKE 兜底。单连接 + 锁。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        _ensure_schema(self._conn)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def _row(self, row) -> dict:
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        d["strengthened"] = bool(d["strengthened"])
        return d

    def create(
        self,
        memory_type: str,
        content: str,
        *,
        confidence: float = 0.5,
        sensitivity: int = 0,
        source: str = "cli",
        metadata: dict | None = None,
    ) -> int:
        if memory_type not in MEMORY_TYPES:
            raise MemoryError(f"unknown memory_type: {memory_type!r}")
        now = time.time()
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (memory_type, content, confidence, sensitivity, source, metadata, created_at, last_access) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (memory_type, content, float(confidence), int(sensitivity), source, meta, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row(row) if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> list[dict]:
        sql = "SELECT * FROM memories WHERE sensitivity >= ?"
        params: list = [int(min_sensitivity)]
        if memory_type is not None:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memories").fetchall()
        return [self._row(r) for r in rows]

    def touch(self, memory_id: int, at: float | None = None) -> None:
        now = time.time() if at is None else at
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_access = ?, access_count = access_count + 1 WHERE id = ?",
                (now, memory_id),
            )
            self._conn.commit()

    def strengthen(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET strengthened = 1, last_access = ? WHERE id = ?",
                (time.time(), memory_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def search(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
    ) -> list[tuple[dict, float]]:
        """返回 [(memory, rank), ...]，rank 高者更相关；LIKE 兜底路径 rank=1.0。"""
        text = (text or "").strip()
        if not text:
            return []
        min_sens = int(min_sensitivity)
        if _uses_fts(text):
            sql = (
                "SELECT m.*, bm25(memories_fts) AS bm25 "
                "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.sensitivity >= ?"
            )
            params: list = [_fts_phrase(text), min_sens]
            if memory_type is not None:
                sql += " AND m.memory_type = ?"
                params.append(memory_type)
            sql += " ORDER BY bm25 LIMIT ?"
            params.append(int(top_k))
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [(self._row(r), 1.0 / (1.0 + abs(r["bm25"]))) for r in rows]
        sql = (
            "SELECT * FROM memories "
            "WHERE content LIKE '%' || ? || '%' AND sensitivity >= ?"
        )
        params = [text, min_sens]
        if memory_type is not None:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(top_k))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(self._row(r), 1.0) for r in rows]

    def wipe(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            self._conn.execute("DELETE FROM memories")
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._conn.commit()
        return n
