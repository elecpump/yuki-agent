from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

_backup_lock = threading.Lock()


def backup_before_migration(
    db_path: Path,
    needs_migration: Callable[[sqlite3.Connection], bool],
) -> Path | None:
    """Create one stable SQLite backup before the first schema-changing migration."""
    if str(db_path) == ":memory:" or not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup_path = db_path.with_name(db_path.name + ".pre-migration.bak")
    with _backup_lock:
        if backup_path.exists():
            return backup_path
        source = sqlite3.connect(str(db_path), timeout=5.0)
        temporary_path: Path | None = None
        try:
            tables = source.execute(
                "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchone()[0]
            if not tables or not needs_migration(source):
                return None
            temporary_path = backup_path.with_name(
                backup_path.name + f".{uuid.uuid4().hex}.tmp"
            )
            destination = sqlite3.connect(str(temporary_path), timeout=5.0)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            temporary_path.replace(backup_path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            source.close()
    return backup_path
