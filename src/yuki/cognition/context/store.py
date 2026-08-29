import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from yuki.memory.manager import MemoryManager

ResponseState = Literal["completed", "failed", "interrupted"]


@dataclass(frozen=True)
class SegmentSummaryJob:
    segment_id: int
    first_turn_id: int
    last_turn_id: int
    attempt: int
    turns: tuple[dict, ...]


class TurnStore(Protocol):
    """会话轮次存储接口（未来 Redis 实现同协议即可替换）。"""

    def add(self, content: str, kind: str, ts: float) -> int | None: ...
    def add_user(
        self,
        content: str,
        *,
        at: float,
        request_id: str | None = None,
    ) -> int | None: ...
    def add_agent(
        self,
        content: str,
        *,
        at: float,
        reply_to_turn_id: int | None = None,
    ) -> int | None: ...
    def mark_response(self, user_turn_id: int, state: ResponseState) -> None: ...
    def items(self) -> list[dict]: ...
    def projection_items(self) -> tuple[list[dict], list[str], list[dict]]: ...
    def close(self) -> None: ...


class ShortTermTurnStore:
    """默认实现：包装 MemoryManager.short_term（TTL 30min/容量 50）。"""

    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    def add(self, content: str, kind: str, ts: float) -> None:
        self._manager.short_term_add(content, kind=kind, at=ts)

    def add_user(
        self,
        content: str,
        *,
        at: float,
        request_id: str | None = None,
    ) -> None:
        del request_id
        self.add(content, "user", at)

    def add_agent(
        self,
        content: str,
        *,
        at: float,
        reply_to_turn_id: int | None = None,
    ) -> None:
        del reply_to_turn_id
        self.add(content, "agent", at)

    def mark_response(self, user_turn_id: int, state: ResponseState) -> None:
        del user_turn_id, state

    def items(self) -> list[dict]:
        return self._manager.short_term_items()

    def projection_items(self) -> tuple[list[dict], list[str], list[dict]]:
        return self.items(), [], []

    def clear(self) -> None:
        self._manager.short_term_clear()

    def close(self) -> None:
        pass


_THREAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    created_at REAL NOT NULL,
    last_turn_at REAL
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    state TEXT NOT NULL CHECK (state IN ('active','closed')),
    summary TEXT,
    summary_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (summary_state IN ('pending','running','ok','placeholder')),
    summary_attempts INTEGER NOT NULL DEFAULT 0,
    summary_lease_until REAL,
    summary_model TEXT,
    summary_prompt_version TEXT,
    first_turn_id INTEGER,
    last_turn_id INTEGER,
    created_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    state TEXT NOT NULL
        CHECK (state IN ('active','closed','consolidating','consolidated')),
    first_user_turn_id INTEGER,
    last_turn_id INTEGER,
    started_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    ended_at REAL,
    consolidated_at REAL
);
CREATE TABLE IF NOT EXISTS thread_turns (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    role TEXT NOT NULL CHECK (role IN ('user','agent')),
    source TEXT NOT NULL CHECK (source IN ('user_input','agent_reply','proactive')),
    content TEXT NOT NULL,
    ts REAL NOT NULL,
    request_id TEXT,
    reply_to_turn_id INTEGER REFERENCES thread_turns(id),
    response_state TEXT
        CHECK (response_state IN ('pending','completed','failed','interrupted')),
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    episode_id INTEGER REFERENCES episodes(id)
);
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL UNIQUE REFERENCES episodes(id),
    state TEXT NOT NULL CHECK (state IN ('pending','leased','completed','failed')),
    lease_until REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    prompt_version TEXT,
    response_json TEXT,
    last_error TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_candidates (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    draft_key TEXT NOT NULL,
    proposed_op TEXT NOT NULL CHECK (proposed_op IN ('add','update','delete')),
    memory_type TEXT NOT NULL CHECK (memory_type IN ('preference','personal','scenario')),
    canonical_key TEXT NOT NULL,
    canonical_key_norm TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL,
    sensitivity INTEGER NOT NULL CHECK (sensitivity IN (0,1,2)),
    target_id INTEGER,
    target_revision INTEGER,
    evidence_json TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate','accepted','rejected','applied')),
    created_at REAL NOT NULL,
    evaluated_at REAL,
    UNIQUE (episode_id, draft_key)
);
CREATE TABLE IF NOT EXISTS memory_history (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create','update','supersede','tombstone')),
    snapshot_json TEXT NOT NULL,
    candidate_id INTEGER REFERENCES memory_candidates(id),
    created_at REAL NOT NULL,
    UNIQUE (memory_id, revision)
);
CREATE TABLE IF NOT EXISTS memory_key_aliases (
    id INTEGER PRIMARY KEY,
    memory_type TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    canonical_norm TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (memory_type, alias_norm, resolver_version)
);
CREATE TABLE IF NOT EXISTS embedding_outbox (
    memory_id INTEGER PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('upsert','delete')),
    queued_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_one_active
ON segments(thread_id) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_segments_state
ON segments(thread_id, state, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_one_active
ON episodes(thread_id) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_episodes_state_activity
ON episodes(state, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_thread_turns_thread_id ON thread_turns(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_thread_turns_episode_id ON thread_turns(episode_id, id);
CREATE INDEX IF NOT EXISTS idx_thread_turns_segment_id ON thread_turns(segment_id, id);
CREATE INDEX IF NOT EXISTS idx_consolidation_runs_state
ON consolidation_runs(state, lease_until, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_state
ON memory_candidates(state, memory_type, canonical_key_norm);
CREATE INDEX IF NOT EXISTS idx_memory_history_memory
ON memory_history(memory_id, revision);
"""


class ThreadTurnStore:
    """SQLite-backed store for the single persistent Thread."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        segment_max_turns: int = 20,
        episode_idle_s: float = 300.0,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._segment_max_turns = max(1, int(segment_max_turns))
        self._episode_idle_s = max(0.0, float(episode_idle_s))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_THREAD_SCHEMA)
        self._migrate_schema()
        self._conn.execute(
            "INSERT OR IGNORE INTO threads (id, created_at) VALUES (1, strftime('%s','now'))"
        )
        self._recover_abandoned_maintenance(time.time())
        self._conn.commit()

    def add_user(self, content: str, *, at: float, request_id: str | None = None) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                segment_id = self._active_segment(at)
                episode_id = self._active_episode(at)
                cursor = self._conn.execute(
                    """
                    INSERT INTO thread_turns (
                        thread_id, role, source, content, ts, request_id,
                        response_state, segment_id, episode_id
                    ) VALUES (1, 'user', 'user_input', ?, ?, ?, 'pending', ?, ?)
                    """,
                    (content, float(at), request_id, segment_id, episode_id),
                )
                turn_id = int(cursor.lastrowid)
                self._record_turn(segment_id, episode_id, turn_id, at, first_user=True)
                self._roll_segment_if_full(segment_id, at)
                self._conn.commit()
                return turn_id
            except Exception:
                self._conn.rollback()
                raise

    def mark_response(self, user_turn_id: int, state: ResponseState) -> None:
        if state not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"invalid response state: {state!r}")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE thread_turns
                SET response_state = ?
                WHERE id = ? AND role = 'user'
                """,
                (state, int(user_turn_id)),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise ValueError(f"unknown user turn: {user_turn_id}")
            self._conn.commit()

    def add_agent(
        self,
        content: str,
        *,
        at: float,
        reply_to_turn_id: int | None = None,
    ) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                segment_id = self._active_segment(at)
                active_episode = self._conn.execute(
                    "SELECT id FROM episodes WHERE thread_id = 1 AND state = 'active'"
                ).fetchone()
                episode_id = int(active_episode["id"]) if active_episode is not None else None
                source = "proactive"
                if reply_to_turn_id is not None:
                    user = self._conn.execute(
                        "SELECT episode_id FROM thread_turns WHERE id = ? AND role = 'user'",
                        (int(reply_to_turn_id),),
                    ).fetchone()
                    if user is None:
                        raise ValueError(f"unknown user turn: {reply_to_turn_id}")
                    episode_id = user["episode_id"]
                    source = "agent_reply"
                cursor = self._conn.execute(
                    """
                    INSERT INTO thread_turns (
                        thread_id, role, source, content, ts, reply_to_turn_id,
                        segment_id, episode_id
                    ) VALUES (1, 'agent', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        content,
                        float(at),
                        reply_to_turn_id,
                        segment_id,
                        episode_id,
                    ),
                )
                turn_id = int(cursor.lastrowid)
                self._record_turn(segment_id, episode_id, turn_id, at)
                self._roll_segment_if_full(segment_id, at)
                if reply_to_turn_id is not None:
                    self._conn.execute(
                        """
                        UPDATE thread_turns
                        SET response_state = 'completed'
                        WHERE id = ? AND response_state = 'pending'
                        """,
                        (int(reply_to_turn_id),),
                    )
                self._conn.commit()
                return turn_id
            except Exception:
                self._conn.rollback()
                raise

    def add(self, content: str, kind: str, ts: float) -> int:
        if kind == "user":
            return self.add_user(content, at=ts)
        return self.add_agent(content, at=ts)

    def items(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM thread_turns WHERE thread_id = 1 ORDER BY id DESC"
            ).fetchall()
        return [self._turn(row) for row in rows]

    def projection_items(self) -> tuple[list[dict], list[str], list[dict]]:
        with self._lock:
            recent_rows = self._conn.execute(
                """
                SELECT t.*
                FROM thread_turns t
                JOIN segments s ON s.id = t.segment_id
                WHERE t.thread_id = 1 AND s.state = 'active'
                ORDER BY t.id DESC
                """
            ).fetchall()
            fallback_rows = self._conn.execute(
                """
                SELECT t.*
                FROM thread_turns t
                JOIN segments s ON s.id = t.segment_id
                WHERE t.thread_id = 1
                  AND s.state = 'closed'
                  AND s.summary_state IN ('pending', 'running', 'placeholder')
                ORDER BY t.id DESC
                """
            ).fetchall()
            summary_rows = self._conn.execute(
                """
                SELECT summary
                FROM segments
                WHERE thread_id = 1 AND state = 'closed' AND summary_state = 'ok'
                ORDER BY id DESC
                """
            ).fetchall()
        return (
            [self._turn(row) for row in recent_rows],
            [str(row["summary"]) for row in summary_rows],
            [self._turn(row) for row in fallback_rows],
        )

    def claim_segment_summary(
        self,
        *,
        at: float | None = None,
        lease_s: float = 30.0,
    ) -> SegmentSummaryJob | None:
        now = time.time() if at is None else float(at)
        lease_until = now + max(0.01, float(lease_s))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                segment = self._conn.execute(
                    """
                    SELECT id, first_turn_id, last_turn_id, summary_attempts
                    FROM segments
                    WHERE thread_id = 1
                      AND state = 'closed'
                      AND (
                          summary_state = 'pending'
                          OR (
                              summary_state = 'running'
                              AND (summary_lease_until IS NULL OR summary_lease_until <= ?)
                          )
                      )
                    ORDER BY id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if segment is None:
                    self._conn.commit()
                    return None
                segment_id = int(segment["id"])
                cursor = self._conn.execute(
                    """
                    UPDATE segments
                    SET summary_state = 'running',
                        summary_attempts = summary_attempts + 1,
                        summary_lease_until = ?
                    WHERE id = ?
                      AND (
                          summary_state = 'pending'
                          OR (
                              summary_state = 'running'
                              AND (summary_lease_until IS NULL OR summary_lease_until <= ?)
                          )
                      )
                    """,
                    (lease_until, segment_id, now),
                )
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    return None
                rows = self._conn.execute(
                    "SELECT * FROM thread_turns WHERE segment_id = ? ORDER BY id",
                    (segment_id,),
                ).fetchall()
                self._conn.commit()
                return SegmentSummaryJob(
                    segment_id=segment_id,
                    first_turn_id=int(segment["first_turn_id"]),
                    last_turn_id=int(segment["last_turn_id"]),
                    attempt=int(segment["summary_attempts"]) + 1,
                    turns=tuple(self._turn(row) for row in rows),
                )
            except Exception:
                self._conn.rollback()
                raise

    def complete_segment_summary(
        self,
        segment_id: int,
        summary: str,
        *,
        model: str,
        prompt_version: str,
        attempt: int,
    ) -> None:
        summary = summary.strip()
        if not summary:
            raise ValueError("segment summary must not be empty")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE segments
                SET summary = ?, summary_state = 'ok',
                    summary_model = ?, summary_prompt_version = ?,
                    summary_lease_until = NULL
                WHERE id = ? AND state = 'closed' AND summary_state = 'running'
                  AND summary_attempts = ?
                """,
                (summary, model, prompt_version, int(segment_id), int(attempt)),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise ValueError(f"segment lease is stale: {segment_id}")
            self._conn.commit()

    def fail_segment_summary(
        self,
        segment_id: int,
        *,
        attempt: int,
        max_failures: int,
    ) -> None:
        max_failures = max(1, int(max_failures))
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE segments
                SET summary_state = CASE
                        WHEN summary_attempts >= ? THEN 'placeholder'
                        ELSE 'pending'
                    END,
                    summary = CASE
                        WHEN summary_attempts >= ? THEN '摘要暂不可用；使用原文回退。'
                        ELSE NULL
                    END,
                    summary_lease_until = NULL
                WHERE id = ? AND state = 'closed' AND summary_state = 'running'
                  AND summary_attempts = ?
                """,
                (max_failures, max_failures, int(segment_id), int(attempt)),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise ValueError(f"segment lease is stale: {segment_id}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(segments)")}
        migrations = {
            "summary_model": "ALTER TABLE segments ADD COLUMN summary_model TEXT",
            "summary_prompt_version": (
                "ALTER TABLE segments ADD COLUMN summary_prompt_version TEXT"
            ),
            "summary_lease_until": (
                "ALTER TABLE segments ADD COLUMN summary_lease_until REAL"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self._conn.execute(statement)

    def _recover_abandoned_maintenance(self, now: float) -> None:
        self._conn.execute(
            """
            UPDATE segments
            SET summary_state = 'pending', summary_lease_until = NULL
            WHERE summary_state = 'running'
              AND (summary_lease_until IS NULL OR summary_lease_until <= ?)
            """,
            (float(now),),
        )
        self._conn.execute(
            """
            UPDATE consolidation_runs
            SET state = 'pending', lease_until = NULL, updated_at = ?
            WHERE state = 'leased' AND (lease_until IS NULL OR lease_until <= ?)
            """,
            (float(now), float(now)),
        )
        self._conn.execute(
            """
            UPDATE episodes
            SET state = 'closed'
            WHERE state = 'consolidating'
              AND NOT EXISTS (
                  SELECT 1
                  FROM consolidation_runs r
                  WHERE r.episode_id = episodes.id
                    AND r.state = 'leased'
                    AND r.lease_until > ?
              )
            """,
            (float(now),),
        )

    def _active_segment(self, at: float) -> int:
        row = self._conn.execute(
            "SELECT id FROM segments WHERE thread_id = 1 AND state = 'active'"
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cursor = self._conn.execute(
            "INSERT INTO segments (thread_id, state, created_at) VALUES (1, 'active', ?)",
            (float(at),),
        )
        return int(cursor.lastrowid)

    def _active_episode(self, at: float) -> int:
        row = self._conn.execute(
            """
            SELECT id, last_activity_at
            FROM episodes
            WHERE thread_id = 1 AND state = 'active'
            """
        ).fetchone()
        if row is not None:
            if float(at) - float(row["last_activity_at"]) <= self._episode_idle_s:
                return int(row["id"])
            self._close_episode(
                int(row["id"]),
                ended_at=float(row["last_activity_at"]),
                updated_at=at,
            )
        cursor = self._conn.execute(
            """
            INSERT INTO episodes (thread_id, state, started_at, last_activity_at)
            VALUES (1, 'active', ?, ?)
            """,
            (float(at), float(at)),
        )
        return int(cursor.lastrowid)

    def close_idle_episode(self, *, at: float) -> int | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT id, last_activity_at
                    FROM episodes
                    WHERE thread_id = 1 AND state = 'active'
                    """
                ).fetchone()
                if row is None or (
                    float(at) - float(row["last_activity_at"]) <= self._episode_idle_s
                ):
                    self._conn.commit()
                    return None
                episode_id = int(row["id"])
                self._close_episode(
                    episode_id,
                    ended_at=float(row["last_activity_at"]),
                    updated_at=at,
                )
                self._conn.commit()
                return episode_id
            except Exception:
                self._conn.rollback()
                raise

    def _close_episode(self, episode_id: int, *, ended_at: float, updated_at: float) -> None:
        cursor = self._conn.execute(
            """
            UPDATE episodes
            SET state = 'closed', ended_at = ?
            WHERE id = ? AND state = 'active'
            """,
            (float(ended_at), int(episode_id)),
        )
        if cursor.rowcount != 1:
            return
        self._conn.execute(
            """
            INSERT INTO consolidation_runs (episode_id, state, updated_at)
            VALUES (?, 'pending', ?)
            ON CONFLICT(episode_id) DO NOTHING
            """,
            (int(episode_id), float(updated_at)),
        )

    def _roll_segment_if_full(self, segment_id: int, at: float) -> None:
        count = self._conn.execute(
            "SELECT count(*) FROM thread_turns WHERE segment_id = ?",
            (segment_id,),
        ).fetchone()[0]
        if int(count) < self._segment_max_turns:
            return
        self._conn.execute(
            "UPDATE segments SET state = 'closed', closed_at = ? WHERE id = ?",
            (float(at), segment_id),
        )
        self._conn.execute(
            "INSERT INTO segments (thread_id, state, created_at) VALUES (1, 'active', ?)",
            (float(at),),
        )

    def _record_turn(
        self,
        segment_id: int,
        episode_id: int | None,
        turn_id: int,
        at: float,
        *,
        first_user: bool = False,
    ) -> None:
        self._conn.execute(
            """
            UPDATE segments
            SET first_turn_id = coalesce(first_turn_id, ?), last_turn_id = ?
            WHERE id = ?
            """,
            (turn_id, turn_id, segment_id),
        )
        if episode_id is not None:
            self._conn.execute(
                """
                UPDATE episodes
                SET first_user_turn_id = CASE
                        WHEN ? THEN coalesce(first_user_turn_id, ?)
                        ELSE first_user_turn_id
                    END,
                    last_turn_id = ?, last_activity_at = ?
                WHERE id = ?
                """,
                (first_user, turn_id, turn_id, float(at), episode_id),
            )
        self._conn.execute(
            "UPDATE threads SET last_turn_at = ? WHERE id = 1",
            (float(at),),
        )

    @staticmethod
    def _turn(row: sqlite3.Row) -> dict:
        turn = dict(row)
        turn["kind"] = turn["role"]
        return turn
