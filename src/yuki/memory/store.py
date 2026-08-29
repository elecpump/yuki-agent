from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from yuki.logger import get_audit_logger, get_logger

logger = get_logger("yuki.memory.store")

MEMORY_TYPES = ("preference", "personal", "scenario", "reflection")


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal storage port for persisted memory lookup and maintenance."""

    @property
    def db_path(self) -> Path | None: ...

    def persist(self) -> None: ...

    def query(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
    ) -> list[dict]: ...

    def vacuum(self) -> None: ...


class MemoryError(Exception):
    """记忆存储错误。"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id            INTEGER PRIMARY KEY,
            memory_type   TEXT NOT NULL
                          CHECK (memory_type IN ('preference','personal','scenario','reflection')),
            content       TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 0.5,
            sensitivity   INTEGER NOT NULL DEFAULT 0 CHECK (sensitivity IN (0,1,2)),
            source        TEXT NOT NULL DEFAULT 'cli',
            metadata      TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL,
            last_access   REAL NOT NULL,
            access_count  INTEGER NOT NULL DEFAULT 0,
            strengthened  INTEGER NOT NULL DEFAULT 0,
            state         TEXT NOT NULL DEFAULT 'active'
                          CHECK (state IN ('active','superseded','tombstoned')),
            revision      INTEGER NOT NULL DEFAULT 1,
            updated_at    REAL NOT NULL DEFAULT 0,
            supersedes_id INTEGER REFERENCES memories(id)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    migrations = {
        "state": (
            "ALTER TABLE memories ADD COLUMN state TEXT NOT NULL DEFAULT 'active' "
            "CHECK (state IN ('active','superseded','tombstoned'))"
        ),
        "revision": "ALTER TABLE memories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
        "updated_at": "ALTER TABLE memories ADD COLUMN updated_at REAL NOT NULL DEFAULT 0",
        "supersedes_id": (
            "ALTER TABLE memories ADD COLUMN supersedes_id INTEGER REFERENCES memories(id)"
        ),
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    conn.execute("UPDATE memories SET updated_at = created_at WHERE updated_at = 0")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
        "content, content='memories', content_rowid='id', tokenize='trigram')"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            provider      TEXT NOT NULL,
            model         TEXT NOT NULL,
            dimension     INTEGER NOT NULL,
            embedding     BLOB NOT NULL,
            content_hash  TEXT NOT NULL,
            updated_at    REAL NOT NULL,
            PRIMARY KEY (memory_id, provider, model, dimension)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model "
        "ON memory_embeddings(provider, model, dimension)"
    )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )


def _uses_fts(text: str) -> bool:
    """最短分词 >=3 字符才用 FTS；否则 trigram 无法匹配，走 LIKE 兜底。"""
    return min((len(tok) for tok in text.split()), default=0) >= 3


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


class MemoryStore(StorageBackend):
    """SQLite 持久化 + FTS5 trigram 检索 + <3 字符 LIKE 兜底。单连接 + 锁。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=5.0
        )
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        _ensure_schema(self._conn)
        self._conn.commit()

    @property
    def db_path(self) -> Path:
        return self._path

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
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise MemoryError(f"corrupt metadata for memory id={d.get('id')}") from exc
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
        if sensitivity not in (0, 1, 2):
            raise MemoryError(f"sensitivity must be 0, 1 or 2, got {sensitivity!r}")
        if not (0.0 <= confidence <= 1.0):
            raise MemoryError(f"confidence must be in [0, 1], got {confidence!r}")
        now = time.time()
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO memories (
                    memory_type, content, confidence, sensitivity, source, metadata,
                    created_at, last_access, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_type,
                    content,
                    float(confidence),
                    int(sensitivity),
                    source,
                    meta,
                    now,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            memory_id = int(cur.lastrowid)
        get_audit_logger().info("memory.create", memory_id=memory_id, memory_type=memory_type)
        return memory_id

    def get(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ? AND state = 'active'",
                (memory_id,),
            ).fetchone()
        return self._row(row) if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def delete_decayed(self, *, last_access_before: float | None = None) -> int:
        sql = (
            "DELETE FROM memories WHERE state = 'active' "
            "AND memory_type != 'personal' AND strengthened = 0"
        )
        params: list = []
        if last_access_before is not None:
            sql += " AND last_access < ?"
            params.append(float(last_access_before))
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
        return cur.rowcount

    def list(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> list[dict]:
        sql = "SELECT * FROM memories WHERE state = 'active' AND sensitivity >= ?"
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
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE state = 'active'"
            ).fetchall()
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

    def upsert_embedding(
        self,
        memory_id: int,
        *,
        provider: str,
        model: str,
        dimension: int,
        embedding: bytes,
        content_hash: str,
        updated_at: float | None = None,
    ) -> None:
        now = time.time() if updated_at is None else updated_at
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_embeddings (
                    memory_id, provider, model, dimension, embedding, content_hash, updated_at
                )
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(memory_id, provider, model, dimension) DO UPDATE SET
                    embedding = excluded.embedding,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    int(memory_id),
                    provider,
                    model,
                    int(dimension),
                    embedding,
                    content_hash,
                    now,
                ),
            )
            self._conn.commit()

    def embedding_metadata(
        self,
        memory_id: int,
        *,
        provider: str,
        model: str,
        dimension: int,
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT memory_id, provider, model, dimension, content_hash, updated_at
                FROM memory_embeddings
                WHERE memory_id = ? AND provider = ? AND model = ? AND dimension = ?
                """,
                (int(memory_id), provider, model, int(dimension)),
            ).fetchone()
        return dict(row) if row else None

    def vector_rows(
        self,
        *,
        provider: str,
        model: str,
        dimension: int,
        memory_type: str | None = None,
        min_sensitivity: int = 0,
    ) -> list[tuple[dict, bytes]]:
        sql = (
            "SELECT m.*, e.embedding FROM memory_embeddings e "
            "JOIN memories m ON m.id = e.memory_id "
            "WHERE e.provider = ? AND e.model = ? AND e.dimension = ? "
            "AND m.state = 'active' AND m.sensitivity >= ?"
        )
        params: list = [provider, model, int(dimension), int(min_sensitivity)]
        if memory_type is not None:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            memory = self._row(row)
            embedding = bytes(memory.pop("embedding"))
            results.append((memory, embedding))
        return results

    def vector_index_state(
        self,
        *,
        provider: str,
        model: str,
        dimension: int,
        memory_type: str | None = None,
        min_sensitivity: int = 0,
    ) -> tuple[int, float | None]:
        sql = (
            "SELECT count(*) AS embedding_count, max(e.updated_at) AS last_updated "
            "FROM memory_embeddings e "
            "JOIN memories m ON m.id = e.memory_id "
            "WHERE e.provider = ? AND e.model = ? AND e.dimension = ? "
            "AND m.state = 'active' AND m.sensitivity >= ?"
        )
        params: list = [provider, model, int(dimension), int(min_sensitivity)]
        if memory_type is not None:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        last_updated = row["last_updated"]
        return (
            int(row["embedding_count"]),
            float(last_updated) if last_updated is not None else None,
        )

    def embeddings_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0])

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
                "WHERE memories_fts MATCH ? AND m.state = 'active' AND m.sensitivity >= ?"
            )
            params: list = [_fts_phrase(text), min_sens]
            if memory_type is not None:
                sql += " AND m.memory_type = ?"
                params.append(memory_type)
            sql += " ORDER BY bm25 LIMIT ?"
            params.append(int(top_k))
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            ranked = []
            for r in rows:
                relevance = max(0.0, -float(r["bm25"]))
                ranked.append((self._row(r), 1.0 - 1.0 / (1.0 + relevance)))
            return ranked
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = (
            "SELECT * FROM memories "
            "WHERE state = 'active' "
            "AND content LIKE '%' || ? || '%' ESCAPE '\\' AND sensitivity >= ?"
        )
        params = [escaped, min_sens]
        if memory_type is not None:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(top_k))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(self._row(r), 1.0) for r in rows]

    def persist(self) -> None:
        with self._lock:
            self._conn.commit()

    def query(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
    ) -> list[dict]:
        return [
            memory
            for memory, _ in self.search(
                text,
                memory_type=memory_type,
                top_k=top_k,
                min_sensitivity=min_sensitivity,
            )
        ]

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def wipe(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            self._conn.execute("DELETE FROM memory_embeddings")
            self._conn.execute("DELETE FROM memories")
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._conn.commit()
        return n
