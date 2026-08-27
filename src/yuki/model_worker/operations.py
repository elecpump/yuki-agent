from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ModelOperation:
    operation_id: str
    idempotency_key: str
    action: str
    model: str | None
    reason: str | None
    state: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error_code: str | None = None
    cancel_requested: bool = False


class ModelOperationStore:
    def __init__(
        self,
        handler: Callable[[str, str | None], dict],
        *,
        ttl_s: float = 300.0,
        queue_size: int = 64,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._handler = handler
        self._ttl_s = ttl_s
        self._clock = clock
        self._operations: dict[str, ModelOperation] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="yuki-model-operations",
        )
        self._thread.start()

    def submit(
        self,
        *,
        idempotency_key: str,
        action: str,
        model: str | None = None,
        reason: str | None = None,
    ) -> dict:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        self.cleanup()
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                return {"operation_id": existing, "accepted": True}
            if self._stop.is_set():
                raise RuntimeError("operation_store_stopped")
            operation = ModelOperation(
                operation_id=uuid.uuid4().hex,
                idempotency_key=idempotency_key,
                action=action,
                model=model,
                reason=reason,
                state="queued",
                created_at=self._clock(),
            )
            self._operations[operation.operation_id] = operation
            self._idempotency[idempotency_key] = operation.operation_id
            try:
                self._queue.put_nowait(operation.operation_id)
            except queue.Full as exc:
                self._operations.pop(operation.operation_id, None)
                self._idempotency.pop(idempotency_key, None)
                raise RuntimeError("operation_queue_full") from exc
            return {"operation_id": operation.operation_id, "accepted": True}

    def status(self, operation_id: str) -> dict:
        self.cleanup()
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise KeyError("operation_not_found")
            result = asdict(operation)
            result.pop("idempotency_key", None)
            result.pop("reason", None)
            result.pop("cancel_requested", None)
            return result

    def cancel(self, operation_id: str) -> dict:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise KeyError("operation_not_found")
            operation.cancel_requested = True
            if operation.state == "queued":
                operation.state = "cancelled"
                operation.finished_at = self._clock()
            return {
                "cancel_requested": True,
                "state": operation.state,
            }

    def cleanup(self) -> None:
        now = self._clock()
        with self._lock:
            expired = [
                operation_id
                for operation_id, operation in self._operations.items()
                if operation.finished_at is not None
                and now - operation.finished_at >= self._ttl_s
            ]
            for operation_id in expired:
                operation = self._operations.pop(operation_id)
                if self._idempotency.get(operation.idempotency_key) == operation_id:
                    self._idempotency.pop(operation.idempotency_key, None)

    def backlog(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> dict:
        return {
            "healthy": self._thread.is_alive() and not self._stop.is_set(),
            "backlog": self.backlog(),
        }

    def close(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            now = self._clock()
            for operation in self._operations.values():
                if operation.state == "queued":
                    operation.cancel_requested = True
                    operation.state = "cancelled"
                    operation.finished_at = now
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                operation_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                self.cleanup()
                continue
            if operation_id is None:
                return
            with self._lock:
                operation = self._operations.get(operation_id)
                if (
                    operation is None
                    or operation.state == "cancelled"
                    or self._stop.is_set()
                ):
                    continue
                operation.state = "running"
                operation.started_at = self._clock()
            try:
                raw_result = self._handler(operation.action, operation.model)
                result = dict(raw_result or {})
            except Exception:
                with self._lock:
                    operation.state = "failed"
                    operation.error_code = "operation_failed"
                    operation.finished_at = self._clock()
            else:
                with self._lock:
                    operation.result = result
                    operation.state = (
                        "cancelled" if operation.cancel_requested else "succeeded"
                    )
                    operation.finished_at = self._clock()
