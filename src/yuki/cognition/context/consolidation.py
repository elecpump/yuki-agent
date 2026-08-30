from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from yuki.cognition.context.sediment import MemoryCandidate, normalize_surface
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.context.consolidation")


@dataclass(frozen=True)
class EvolutionPolicy:
    promotion_min_episodes: int = 2
    strengthen_min_episodes: int = 3
    tombstone_min_episodes: int = 2
    update_min_episodes: int = 3
    explicit_activation_confidence: float = 0.9


@dataclass(frozen=True)
class ConsolidationJob:
    run_id: int
    episode_id: int
    attempt: int
    turns: list[dict]
    related: list[dict]


class CandidateResolver:
    """Conservatively map a drifting suggested key to one existing identity."""

    version = "candidate-resolver-v1"

    def __init__(
        self,
        *,
        similarity: Callable[[str, str], float] | None = None,
        threshold: float = 0.88,
        competition_margin: float = 0.03,
    ) -> None:
        self._similarity = similarity or self._surface_similarity
        self.threshold = float(threshold)
        self.competition_margin = float(competition_margin)

    def resolve(
        self,
        proposed: str,
        content: str,
        existing: dict[str, list[str]],
    ) -> str | None:
        if proposed in existing:
            return proposed
        ranked = []
        for identity, contents in existing.items():
            key_score = float(self._similarity(proposed, identity))
            content_score = max(
                (float(self._similarity(content, item)) for item in contents),
                default=0.0,
            )
            ranked.append((max(key_score, content_score), identity))
        ranked.sort(reverse=True)
        if not ranked or ranked[0][0] < self.threshold:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < self.competition_margin:
            return None
        return ranked[0][1]

    @staticmethod
    def _surface_similarity(left: str, right: str) -> float:
        return SequenceMatcher(None, left, right).ratio()


class ConsolidationStore:
    """Lease and atomically apply memory candidates in the shared SQLite database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        policy: EvolutionPolicy | None = None,
        resolver: CandidateResolver | None = None,
        fault_injector: Callable[[str], None] | None = None,
        related_provider: Callable[[list[dict], int], list[dict]] | None = None,
    ) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self.policy = policy or EvolutionPolicy()
        self.resolver = resolver or CandidateResolver()
        self.fault_injector = fault_injector
        self._related_provider = related_provider

    def claim(
        self,
        *,
        at: float | None = None,
        lease_s: float = 30.0,
        related_limit: int = 8,
    ) -> ConsolidationJob | None:
        now = time.time() if at is None else float(at)
        lease_until = now + max(0.01, float(lease_s))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT r.id, r.episode_id, r.attempt_count
                    FROM consolidation_runs r
                    JOIN episodes e ON e.id = r.episode_id
                    WHERE e.state IN ('closed', 'consolidating')
                      AND (
                          r.state = 'pending'
                          OR (r.state = 'leased' AND (r.lease_until IS NULL OR r.lease_until <= ?))
                      )
                    ORDER BY r.episode_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                run_id = int(row["id"])
                cursor = self._conn.execute(
                    """
                    UPDATE consolidation_runs
                    SET state = 'leased', lease_until = ?,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE id = ? AND (
                        state = 'pending'
                        OR (state = 'leased' AND (lease_until IS NULL OR lease_until <= ?))
                    )
                    """,
                    (lease_until, now, run_id, now),
                )
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    return None
                episode_id = int(row["episode_id"])
                self._conn.execute(
                    "UPDATE episodes SET state = 'consolidating' WHERE id = ?",
                    (episode_id,),
                )
                turns = [
                    self._turn(item)
                    for item in self._conn.execute(
                        "SELECT * FROM thread_turns WHERE episode_id = ? ORDER BY id",
                        (episode_id,),
                    ).fetchall()
                ]
                fallback_related = [
                    self._memory(item)
                    for item in self._conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE state = 'active'
                        ORDER BY strengthened DESC, updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (max(0, int(related_limit)),),
                    ).fetchall()
                ]
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        related = fallback_related
        if self._related_provider is not None:
            try:
                provided = self._related_provider(turns, max(0, int(related_limit)))
                related = [
                    item
                    for item in provided
                    if item is not None and item.get("state", "active") == "active"
                ][:related_limit]
            except Exception:
                logger.warning("related memory retrieval failed", exc_info=True)
        return ConsolidationJob(
            run_id=run_id,
            episode_id=episode_id,
            attempt=int(row["attempt_count"]) + 1,
            turns=turns,
            related=related,
        )

    def release(
        self,
        job: ConsolidationJob,
        error: str,
        *,
        at: float | None = None,
        failed: bool = False,
    ) -> None:
        now = time.time() if at is None else float(at)
        next_state = "failed" if failed else "pending"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE consolidation_runs
                    SET state = ?, lease_until = NULL, last_error = ?, updated_at = ?
                    WHERE id = ? AND state = 'leased' AND attempt_count = ?
                    """,
                    (next_state, error[:2000], now, job.run_id, job.attempt),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"consolidation lease is stale: {job.run_id}")
                self._conn.execute(
                    "UPDATE episodes SET state = 'closed' WHERE id = ? AND state = 'consolidating'",
                    (job.episode_id,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def complete(
        self,
        job: ConsolidationJob,
        candidates: list[MemoryCandidate],
        *,
        model: str = "",
        prompt_version: str = "sediment-v1",
        response_json: str | None = None,
        at: float | None = None,
    ) -> None:
        now = time.time() if at is None else float(at)
        response_json = response_json or json.dumps(
            {"candidates": [candidate.as_dict() for candidate in candidates]},
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                lease = self._conn.execute(
                    """
                    SELECT id FROM consolidation_runs
                    WHERE id = ? AND state = 'leased' AND attempt_count = ?
                    """,
                    (job.run_id, job.attempt),
                ).fetchone()
                if lease is None:
                    raise ValueError(f"consolidation lease is stale: {job.run_id}")
                for candidate in candidates:
                    resolved = self._resolve_candidate(candidate, now=now)
                    candidate_id = self._insert_candidate(job.episode_id, resolved, now=now)
                    self._inject_fault("after_candidate_insert")
                    self._evolve(candidate_id, resolved, now=now)
                    self._inject_fault("after_memory_evolution")
                self._inject_fault("before_watermark")
                self._conn.execute(
                    """
                    UPDATE consolidation_runs
                    SET state = 'completed', lease_until = NULL, model = ?,
                        prompt_version = ?, response_json = ?, last_error = NULL, updated_at = ?
                    WHERE id = ? AND state = 'leased' AND attempt_count = ?
                    """,
                    (model, prompt_version, response_json, now, job.run_id, job.attempt),
                )
                self._conn.execute(
                    """
                    UPDATE episodes
                    SET state = 'consolidated', consolidated_at = ?
                    WHERE id = ? AND state = 'consolidating'
                    """,
                    (now, job.episode_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _inject_fault(self, boundary: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(boundary)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _resolve_candidate(self, candidate: MemoryCandidate, *, now: float) -> MemoryCandidate:
        proposed = candidate.canonical_key_norm
        alias = self._conn.execute(
            """
            SELECT canonical_norm FROM memory_key_aliases
            WHERE memory_type = ? AND alias_norm = ? AND resolver_version = ?
            """,
            (candidate.memory_type, proposed, self.resolver.version),
        ).fetchone()
        if alias is not None:
            return replace(candidate, canonical_key_norm=str(alias["canonical_norm"]))
        existing = self._existing_identities(candidate.memory_type)
        resolved = self.resolver.resolve(
            proposed,
            normalize_surface(candidate.content),
            existing,
        )
        if resolved is None or resolved == proposed:
            return candidate
        self._conn.execute(
            """
            INSERT OR IGNORE INTO memory_key_aliases (
                memory_type, alias_norm, canonical_norm, resolver_version, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (candidate.memory_type, proposed, resolved, self.resolver.version, now),
        )
        return replace(candidate, canonical_key_norm=resolved)

    def _existing_identities(self, memory_type: str) -> dict[str, list[str]]:
        identities: dict[str, list[str]] = {}
        for row in self._conn.execute(
            """
            SELECT canonical_key_norm, content
            FROM memory_candidates
            WHERE memory_type = ? AND state != 'rejected'
            """,
            (memory_type,),
        ).fetchall():
            identities.setdefault(str(row["canonical_key_norm"]), []).append(
                normalize_surface(str(row["content"]))
            )
        rows = self._conn.execute(
            "SELECT metadata, content FROM memories WHERE memory_type = ?",
            (memory_type,),
        ).fetchall()
        for row in rows:
            metadata = self._metadata(row["metadata"])
            norm = str(metadata.get("canonical_key_norm", ""))
            if norm:
                identities.setdefault(norm, []).append(normalize_surface(str(row["content"])))
        return identities

    def _insert_candidate(
        self,
        episode_id: int,
        candidate: MemoryCandidate,
        *,
        now: float,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO memory_candidates (
                episode_id, draft_key, proposed_op, memory_type, canonical_key,
                canonical_key_norm, content, confidence, sensitivity, target_id,
                target_revision, evidence_json, metadata, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
            ON CONFLICT(episode_id, draft_key) DO NOTHING
            """,
            (
                episode_id,
                candidate.draft_key,
                candidate.proposed_op,
                candidate.memory_type,
                candidate.canonical_key,
                candidate.canonical_key_norm,
                candidate.content,
                candidate.confidence,
                candidate.sensitivity,
                candidate.target_id,
                candidate.target_revision,
                json.dumps([item.__dict__ for item in candidate.evidence], ensure_ascii=False),
                json.dumps(candidate.metadata, ensure_ascii=False),
                now,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid)
        row = self._conn.execute(
            "SELECT id FROM memory_candidates WHERE episode_id = ? AND draft_key = ?",
            (episode_id, candidate.draft_key),
        ).fetchone()
        return int(row["id"])

    def _evolve(self, candidate_id: int, candidate: MemoryCandidate, *, now: float) -> None:
        support = self._support_count(candidate)
        if candidate.proposed_op == "add":
            self._evolve_add(candidate_id, candidate, support=support, now=now)
        elif candidate.proposed_op == "update":
            self._evolve_update(candidate_id, candidate, support=support, now=now)
        else:
            self._evolve_delete(candidate_id, candidate, support=support, now=now)

    def _evolve_add(
        self,
        candidate_id: int,
        candidate: MemoryCandidate,
        *,
        support: int,
        now: float,
    ) -> None:
        existing = self._find_active(candidate.memory_type, candidate.canonical_key_norm)
        if existing is not None:
            if self._same_content(existing["content"], candidate.content):
                should_strengthen = (
                    candidate.memory_type == "preference"
                    and support >= self.policy.strengthen_min_episodes
                )
                if should_strengthen:
                    self._conn.execute(
                        "UPDATE memories SET strengthened = 1, updated_at = ? WHERE id = ?",
                        (now, int(existing["id"])),
                    )
                self._accept_support(candidate, candidate_id)
            return
        activate = candidate.memory_type == "scenario"
        activate = activate or candidate.confidence >= self.policy.explicit_activation_confidence
        activate = activate or support >= self.policy.promotion_min_episodes
        if not activate:
            return
        memory_id = self._create_memory(candidate, revision=1, supersedes_id=None, now=now)
        memory = self._raw_memory(memory_id)
        self._history(memory, "create", candidate_id, now=now)
        self._queue_embedding(memory_id, "upsert", now=now)
        self._accept_support(candidate, candidate_id)

    def _evolve_update(
        self,
        candidate_id: int,
        candidate: MemoryCandidate,
        *,
        support: int,
        now: float,
    ) -> None:
        if support < self.policy.update_min_episodes:
            return
        target = self._valid_target(candidate)
        if target is None or self._memory_norm(target) != candidate.canonical_key_norm:
            self._reject(candidate_id, now=now)
            return
        next_revision = int(target["revision"]) + 1
        old = target
        self._conn.execute(
            """
            UPDATE memories
            SET state = 'superseded', updated_at = ?
            WHERE id = ? AND state = 'active' AND revision = ?
            """,
            (now, int(target["id"]), int(candidate.target_revision)),
        )
        self._history(
            old,
            "supersede",
            candidate_id,
            history_revision=next_revision,
            now=now,
        )
        memory_id = self._create_memory(
            candidate,
            revision=next_revision,
            supersedes_id=int(target["id"]),
            now=now,
        )
        self._history(self._raw_memory(memory_id), "update", candidate_id, now=now)
        self._queue_embedding(int(target["id"]), "delete", now=now)
        self._queue_embedding(memory_id, "upsert", now=now)
        self._accept_support(candidate, candidate_id)

    def _evolve_delete(
        self,
        candidate_id: int,
        candidate: MemoryCandidate,
        *,
        support: int,
        now: float,
    ) -> None:
        if support < self.policy.tombstone_min_episodes:
            return
        target = self._valid_target(candidate)
        if target is None or self._memory_norm(target) != candidate.canonical_key_norm:
            self._reject(candidate_id, now=now)
            return
        next_revision = int(target["revision"]) + 1
        old = target
        self._conn.execute(
            """
            UPDATE memories
            SET state = 'tombstoned', updated_at = ?
            WHERE id = ? AND state = 'active' AND revision = ?
            """,
            (now, int(target["id"]), int(candidate.target_revision)),
        )
        self._history(
            old,
            "tombstone",
            candidate_id,
            history_revision=next_revision,
            now=now,
        )
        self._queue_embedding(int(target["id"]), "delete", now=now)
        self._accept_support(candidate, candidate_id)

    def _support_count(self, candidate: MemoryCandidate) -> int:
        rows = self._conn.execute(
            """
            SELECT episode_id, content FROM memory_candidates
            WHERE memory_type = ? AND canonical_key_norm = ? AND proposed_op = ?
              AND state != 'rejected'
            """,
            (candidate.memory_type, candidate.canonical_key_norm, candidate.proposed_op),
        ).fetchall()
        return len(
            {
                int(row["episode_id"])
                for row in rows
                if self._same_content(str(row["content"]), candidate.content)
            }
        )

    def _find_active(self, memory_type: str, canonical_norm: str) -> sqlite3.Row | None:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE state = 'active' AND memory_type = ?",
            (memory_type,),
        ).fetchall()
        return next((row for row in rows if self._memory_norm(row) == canonical_norm), None)

    def _valid_target(self, candidate: MemoryCandidate) -> sqlite3.Row | None:
        if candidate.target_id is None or candidate.target_revision is None:
            return None
        return self._conn.execute(
            """
            SELECT * FROM memories
            WHERE id = ? AND state = 'active' AND revision = ? AND memory_type = ?
            """,
            (candidate.target_id, candidate.target_revision, candidate.memory_type),
        ).fetchone()

    def _create_memory(
        self,
        candidate: MemoryCandidate,
        *,
        revision: int,
        supersedes_id: int | None,
        now: float,
    ) -> int:
        metadata = dict(candidate.metadata)
        metadata.update(
            {
                "canonical_key": candidate.canonical_key,
                "canonical_key_norm": candidate.canonical_key_norm,
            }
        )
        cursor = self._conn.execute(
            """
            INSERT INTO memories (
                memory_type, content, confidence, sensitivity, source, metadata,
                created_at, last_access, updated_at, revision, supersedes_id
            ) VALUES (?, ?, ?, ?, 'sediment', ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.memory_type,
                candidate.content,
                candidate.confidence,
                candidate.sensitivity,
                json.dumps(metadata, ensure_ascii=False),
                now,
                now,
                now,
                revision,
                supersedes_id,
            ),
        )
        return int(cursor.lastrowid)

    def _accept_support(self, candidate: MemoryCandidate, candidate_id: int) -> None:
        self._conn.execute(
            """
            UPDATE memory_candidates
            SET state = 'accepted', evaluated_at = coalesce(evaluated_at, created_at)
            WHERE memory_type = ? AND canonical_key_norm = ? AND proposed_op = ?
              AND state = 'candidate'
            """,
            (candidate.memory_type, candidate.canonical_key_norm, candidate.proposed_op),
        )
        self._conn.execute(
            "UPDATE memory_candidates SET state = 'applied' WHERE id = ?",
            (candidate_id,),
        )

    def _reject(self, candidate_id: int, *, now: float) -> None:
        self._conn.execute(
            "UPDATE memory_candidates SET state = 'rejected', evaluated_at = ? WHERE id = ?",
            (now, candidate_id),
        )

    def _history(
        self,
        memory: sqlite3.Row,
        operation: str,
        candidate_id: int,
        *,
        history_revision: int | None = None,
        now: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO memory_history (
                memory_id, revision, operation, snapshot_json, candidate_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(memory["id"]),
                int(memory["revision"]) if history_revision is None else int(history_revision),
                operation,
                json.dumps(dict(memory), ensure_ascii=False),
                candidate_id,
                now,
            ),
        )

    def _queue_embedding(self, memory_id: int, operation: str, *, now: float) -> None:
        self._conn.execute(
            """
            INSERT INTO embedding_outbox (memory_id, operation, queued_at)
            VALUES (?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                operation = excluded.operation, queued_at = excluded.queued_at
            """,
            (memory_id, operation, now),
        )

    def _raw_memory(self, memory_id: int) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"memory not found: {memory_id}")
        return row

    def _memory_norm(self, memory: sqlite3.Row) -> str:
        return str(self._metadata(memory["metadata"]).get("canonical_key_norm", ""))

    @staticmethod
    def _same_content(left: str, right: str) -> bool:
        return normalize_surface(left) == normalize_surface(right)

    @staticmethod
    def _metadata(raw: str) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _memory(cls, row: sqlite3.Row) -> dict:
        memory = dict(row)
        memory["metadata"] = cls._metadata(memory.get("metadata", "{}"))
        memory["strengthened"] = bool(memory.get("strengthened"))
        return memory

    @staticmethod
    def _turn(row: sqlite3.Row) -> dict:
        turn = dict(row)
        turn["kind"] = turn["role"]
        return turn
