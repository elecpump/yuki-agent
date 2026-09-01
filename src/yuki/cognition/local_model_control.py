from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from yuki.bus import BusError, BusTimeoutError


class LocalModelRegistryProtocol(Protocol):
    def get_model_health(self, model: str) -> dict: ...

    def set_local_chat_enabled(
        self,
        enabled: bool,
        *,
        idempotency_key: str,
    ) -> dict: ...

    def operation_status(self, operation_id: str) -> dict: ...


class LocalRouteGateProtocol(Protocol):
    def set_local_enabled(self, enabled: bool) -> None: ...

    def local_enabled(self) -> bool: ...


class LocalModelControlError(Exception):
    """Stable domain error raised by the local-chat control boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class _Operation:
    operation_id: str
    idempotency_key: str
    target_enabled: bool
    state: str = "queued"
    worker_operation_id: str | None = None
    error_code: str | None = None
    finished_at: float | None = None
    verify_first: bool = False
    public_visible: bool = True

    def public(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "target_enabled": self.target_enabled,
            "state": self.state,
            "error_code": self.error_code,
        }


class LocalChatControl:
    """Keep local-chat routing and worker residency converged to one target."""

    def __init__(
        self,
        registry: LocalModelRegistryProtocol,
        hub: LocalRouteGateProtocol,
        *,
        available: bool,
        initially_enabled: bool | None = None,
        retry_delays: Sequence[float] = (0.05, 0.1, 0.25, 0.5, 1.0),
        wait: Callable[[threading.Event, float], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        operation_ttl_s: float = 300.0,
    ) -> None:
        self._registry = registry
        self._hub = hub
        self._available = bool(available)
        initial = self._available if initially_enabled is None else bool(initially_enabled)
        self._target_enabled = self._available and initial
        self._lock = threading.RLock()
        self._operations: dict[str, _Operation] = {}
        self._idempotency: dict[str, str] = {}
        self._active_operation_id: str | None = None
        self._last_operation_id: str | None = None
        self._last_error = ""
        self._health = {
            "runtime_state": "unknown" if self._available else "disabled",
            "runtime_enabled": False,
            "callable": False,
            "loaded": False,
            "active_calls": 0,
        }
        delays = tuple(max(0.001, float(delay)) for delay in retry_delays)
        self._retry_delays = delays or (0.1,)
        self._retry_index = 0
        self._wait = wait or (lambda event, delay: event.wait(delay))
        self._clock = clock
        self._operation_ttl_s = max(0.0, float(operation_ttl_s))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        if self._available:
            self._hub.set_local_enabled(False)
            bootstrap = _Operation(
                operation_id=uuid.uuid4().hex,
                idempotency_key=f"bootstrap:{uuid.uuid4().hex}",
                target_enabled=self._target_enabled,
                state="recovering",
                verify_first=True,
                public_visible=False,
            )
            self._operations[bootstrap.operation_id] = bootstrap
            self._idempotency[bootstrap.idempotency_key] = bootstrap.operation_id
            self._active_operation_id = bootstrap.operation_id
            self._last_operation_id = bootstrap.operation_id
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="yuki-local-chat-control",
            )
            self._thread.start()

    def status(self) -> dict:
        with self._lock:
            self._cleanup_locked()
            if not self._available:
                return {
                    "available": False,
                    "enabled": False,
                    "target_enabled": False,
                    "state": "unavailable",
                    "runtime_state": "disabled",
                    "loaded": False,
                    "active_calls": 0,
                    "operation": None,
                    "last_error": "",
                }
            operation = self._current_operation_locked()
            active = (
                self._operations.get(self._active_operation_id)
                if self._active_operation_id is not None
                else None
            )
            return {
                "available": True,
                "enabled": bool(self._hub.local_enabled()),
                "target_enabled": self._target_enabled,
                "state": self._state_locked(operation),
                "runtime_state": str(self._health.get("runtime_state") or "unknown"),
                "loaded": bool(self._health.get("loaded", False)),
                "active_calls": int(self._health.get("active_calls") or 0),
                "operation": (
                    active.public()
                    if active is not None and active.public_visible
                    else None
                ),
                "last_error": self._last_error,
            }

    def set_enabled(self, enabled: bool, idempotency_key: str) -> dict:
        if not self._available:
            raise LocalModelControlError("local_model_config_disabled")
        target = bool(enabled)
        with self._lock:
            self._cleanup_locked()
            existing_id = self._idempotency.get(str(idempotency_key))
            if existing_id is not None:
                existing = self._operations[existing_id]
                if existing.target_enabled != target:
                    raise LocalModelControlError("idempotency_key_conflict")
                return self._accepted(existing)
            if self._active_operation_id is not None:
                active = self._operations[self._active_operation_id]
                if active.verify_first:
                    active.target_enabled = target
                    active.idempotency_key = str(idempotency_key)
                    active.public_visible = True
                    self._idempotency[str(idempotency_key)] = active.operation_id
                    self._target_enabled = target
                    self._hub.set_local_enabled(False)
                    return self._accepted(active)
                if active.target_enabled != target:
                    raise LocalModelControlError("local_model_operation_in_progress")
                self._idempotency[str(idempotency_key)] = active.operation_id
                return self._accepted(active)
            operation = _Operation(uuid.uuid4().hex, str(idempotency_key), target)
            self._operations[operation.operation_id] = operation
            self._idempotency[operation.idempotency_key] = operation.operation_id
            self._active_operation_id = operation.operation_id
            self._last_operation_id = operation.operation_id
            self._target_enabled = target
            self._last_error = ""
            self._hub.set_local_enabled(False)
        self._wake.set()
        return self._accepted(operation)

    def operation_status(self, operation_id: str) -> dict:
        with self._lock:
            self._cleanup_locked()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise LocalModelControlError("local_model_operation_not_found")
            return operation.public()

    def close(self, timeout_s: float = 1.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_s))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile_once()
            except BusTimeoutError:
                self._mark_recovering("model_worker_timeout")
            except BusError:
                self._mark_recovering("model_worker_unavailable")
            except Exception:
                self._mark_failed("operation_failed")
            self._wake.clear()
            delay = self._retry_delays[min(self._retry_index, len(self._retry_delays) - 1)]
            self._wait(self._wake, delay)

    def _mark_recovering(self, error_code: str) -> None:
        with self._lock:
            operation = self._current_operation_locked()
            if operation is None or operation.state == "failed":
                return
            operation.state = "recovering"
            operation.error_code = error_code
            operation.finished_at = None
            operation.worker_operation_id = None
            self._last_error = error_code
            self._hub.set_local_enabled(False)
            self._active_operation_id = operation.operation_id
            self._retry_index = min(self._retry_index + 1, len(self._retry_delays) - 1)

    def _mark_failed(self, error_code: str) -> None:
        with self._lock:
            operation = self._current_operation_locked()
            if operation is None or operation.state in {"succeeded", "failed"}:
                return
            operation.state = "failed"
            operation.error_code = error_code
            operation.finished_at = self._clock()
            self._last_error = error_code
            self._hub.set_local_enabled(False)
            self._active_operation_id = None

    def _reconcile_once(self) -> None:
        with self._lock:
            operation = self._current_operation_locked()
            if operation is None or operation.state == "failed":
                return
            if operation.verify_first:
                target = operation.target_enabled
                verify_first = True
                check_drift = False
                worker_operation_id = ""
                key = ""
            else:
                verify_first = False
            if not verify_first and operation.state == "succeeded":
                target = operation.target_enabled
                check_drift = True
            elif not verify_first:
                check_drift = False
            if verify_first or check_drift:
                worker_operation_id = ""
                key = ""
            elif operation.worker_operation_id is None:
                operation.state = "running"
                target = operation.target_enabled
                key = operation.idempotency_key
            else:
                target = operation.target_enabled
                key = ""
                worker_operation_id = operation.worker_operation_id

        if verify_first:
            health = dict(self._registry.get_model_health("local_chat"))
            with self._lock:
                if (
                    self._active_operation_id != operation.operation_id
                    or operation.target_enabled != target
                    or self._target_enabled != target
                ):
                    return
                self._health = health
                converged = (
                    bool(health.get("callable", False))
                    if target
                    else (
                        health.get("runtime_state") == "disabled"
                        and not bool(health.get("runtime_enabled", False))
                    )
                )
                if converged:
                    if target:
                        self._hub.set_local_enabled(True)
                    operation.state = "succeeded"
                    operation.error_code = None
                    operation.finished_at = self._clock()
                    operation.verify_first = False
                    self._active_operation_id = None
                    self._last_error = ""
                    return
                operation.verify_first = False
            return

        if check_drift:
            health = dict(self._registry.get_model_health("local_chat"))
            with self._lock:
                if (
                    self._current_operation_locked() is not operation
                    or operation.target_enabled != target
                    or self._target_enabled != target
                ):
                    return
                self._health = health
                converged = (
                    bool(health.get("callable", False))
                    if target
                    else (
                        health.get("runtime_state") == "disabled"
                        and not bool(health.get("runtime_enabled", False))
                    )
                )
                if converged:
                    if target:
                        self._hub.set_local_enabled(True)
                    self._last_error = ""
                    return
                self._hub.set_local_enabled(False)
                operation.state = "recovering"
                operation.error_code = "model_worker_unavailable"
                operation.worker_operation_id = None
                operation.finished_at = None
                self._active_operation_id = operation.operation_id
                self._retry_index = min(self._retry_index + 1, len(self._retry_delays) - 1)
            return

        if key:
            result = self._registry.set_local_chat_enabled(target, idempotency_key=key)
            with self._lock:
                operation.worker_operation_id = str(result["operation_id"])
            return

        result = self._registry.operation_status(worker_operation_id)
        state = str(result.get("state") or "")
        if state == "cancelled" or result.get("error_code") == "operation_not_found":
            self._mark_recovering("model_worker_unavailable")
            return
        if state in {"queued", "running"}:
            return
        if state == "succeeded":
            health = dict(self._registry.get_model_health("local_chat"))
            with self._lock:
                self._health = health
                if target:
                    if not health.get("callable", False):
                        return
                    self._hub.set_local_enabled(True)
                elif not (
                    health.get("runtime_state") == "disabled"
                    and not bool(health.get("runtime_enabled", False))
                ):
                    return
                operation.state = "succeeded"
                operation.error_code = None
                operation.finished_at = self._clock()
                self._active_operation_id = None
                self._retry_index = 0
                self._last_error = ""
            return
        with self._lock:
            operation.state = "failed"
            operation.error_code = str(result.get("error_code") or state or "operation_failed")
            operation.finished_at = self._clock()
            self._last_error = operation.error_code
            self._active_operation_id = None

    def _current_operation_locked(self) -> _Operation | None:
        operation_id = self._active_operation_id or self._last_operation_id
        return self._operations.get(operation_id) if operation_id else None

    def _cleanup_locked(self) -> None:
        now = self._clock()
        expired = {
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.finished_at is not None
            and now - operation.finished_at >= self._operation_ttl_s
        }
        if not expired:
            return
        for operation_id in expired:
            self._operations.pop(operation_id, None)
        self._idempotency = {
            key: operation_id
            for key, operation_id in self._idempotency.items()
            if operation_id not in expired
        }
        if self._last_operation_id in expired:
            self._last_operation_id = None
        if self._active_operation_id in expired:
            self._active_operation_id = None
        if self._available and self._last_operation_id is None:
            anchor = _Operation(
                operation_id=uuid.uuid4().hex,
                idempotency_key=f"reconcile:{uuid.uuid4().hex}",
                target_enabled=self._target_enabled,
                state="succeeded",
                public_visible=False,
            )
            self._operations[anchor.operation_id] = anchor
            self._last_operation_id = anchor.operation_id

    @staticmethod
    def _accepted(operation: _Operation) -> dict:
        return {
            "operation_id": operation.operation_id,
            "accepted": True,
            "target_enabled": operation.target_enabled,
        }

    def _state_locked(self, operation: _Operation | None) -> str:
        if operation is not None and operation.state == "failed":
            return "failed"
        if self._active_operation_id is not None:
            if operation is not None and operation.state == "recovering" and self._target_enabled:
                return "recovering"
            return "enabling" if self._target_enabled else "disabling"
        return "enabled" if self._hub.local_enabled() else "disabled"
