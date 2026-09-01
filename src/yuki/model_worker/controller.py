from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ModelReadinessState(str, Enum):
    DISABLED = "disabled"
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    DRAINING = "draining"
    UNLOADING = "unloading"


@dataclass(frozen=True)
class ManagedModelSpec:
    name: str
    loader: Callable[[], Any]
    unloader: Callable[[Any], None] | None = None
    health_check: Callable[[], dict] | None = None
    preflight_check: Callable[[], dict] | None = None
    dependencies: tuple[str, ...] = ()
    enabled: bool = True
    manual_unload_allowed: bool = True
    priority: int = 50
    warmup: bool = False
    evictable: bool = True
    pinned: bool = False
    idle_unload_s: float = 0.0
    min_residency_s: float = 30.0
    estimated_vram_mb: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("model name is required")
        if not 0 <= self.priority <= 100:
            raise ValueError("model priority must be between 0 and 100")


@dataclass
class ModelRuntimeState:
    state: ModelReadinessState
    active_calls: int = 0
    accepting_calls: bool = False
    last_used_at: float | None = None
    loaded_at: float | None = None
    failure_count: int = 0
    success_count: int = 0
    retry_after: float | None = None
    last_error_code: str | None = None


class ModelUnavailableError(RuntimeError):
    pass


class ModelDrainTimeoutError(RuntimeError):
    pass


class ModelController:
    def __init__(
        self,
        spec: ManagedModelSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self._clock = clock
        initial = (
            ModelReadinessState.UNLOADED
            if spec.enabled
            else ModelReadinessState.DISABLED
        )
        self._runtime = ModelRuntimeState(state=initial)
        self._handle: Any = None
        self._condition = threading.Condition(threading.RLock())
        self._lifecycle_lock = threading.RLock()

    @property
    def handle(self) -> Any:
        with self._condition:
            return self._handle

    def enable(self) -> None:
        with self._lifecycle_lock:
            with self._condition:
                if self._runtime.state == ModelReadinessState.DISABLED or (
                    self._runtime.state == ModelReadinessState.FAILED
                    and self._handle is None
                ):
                    self._runtime.state = ModelReadinessState.UNLOADED
                    self._runtime.last_error_code = None
                    self._condition.notify_all()

    def disable(self, *, timeout_s: float) -> None:
        with self._lifecycle_lock:
            with self._condition:
                if self._runtime.state == ModelReadinessState.DISABLED:
                    return
                if self._handle is None:
                    self._runtime.state = ModelReadinessState.DISABLED
                    self._runtime.accepting_calls = False
                    self._runtime.retry_after = None
                    self._runtime.last_error_code = None
                    self._condition.notify_all()
                    return
            self.unload(timeout_s=timeout_s)
            with self._condition:
                self._runtime.state = ModelReadinessState.DISABLED
                self._runtime.accepting_calls = False
                self._condition.notify_all()

    def load(self) -> Any:
        with self._lifecycle_lock:
            with self._condition:
                if self._runtime.state == ModelReadinessState.DISABLED:
                    raise ModelUnavailableError(f"model disabled: {self.spec.name}")
                if self._runtime.state in {
                    ModelReadinessState.READY,
                    ModelReadinessState.DEGRADED,
                }:
                    return self._handle
                if (
                    self._runtime.state == ModelReadinessState.FAILED
                    and self._handle is not None
                ):
                    raise ModelUnavailableError(
                        f"model failed with retained handle: {self.spec.name}"
                    )
                if self._runtime.state in {
                    ModelReadinessState.DRAINING,
                    ModelReadinessState.UNLOADING,
                }:
                    raise ModelUnavailableError(
                        f"model is draining: {self.spec.name}"
                    )
                self._runtime.state = ModelReadinessState.LOADING
                self._runtime.accepting_calls = False
                self._runtime.last_error_code = None
            try:
                handle = self.spec.loader()
            except Exception:
                with self._condition:
                    self._runtime.state = ModelReadinessState.FAILED
                    self._runtime.failure_count += 1
                    self._runtime.last_error_code = "load_failed"
                    self._condition.notify_all()
                raise
            with self._condition:
                now = self._clock()
                self._handle = handle
                self._runtime.state = ModelReadinessState.READY
                self._runtime.accepting_calls = True
                self._runtime.loaded_at = now
                self._runtime.last_used_at = now
                self._runtime.retry_after = None
                self._condition.notify_all()
                return handle

    def unload(self, *, timeout_s: float, force_manual: bool = False) -> bool:
        with self._lifecycle_lock:
            drain_timed_out = False
            with self._condition:
                if force_manual and not self.spec.manual_unload_allowed:
                    raise ModelUnavailableError(
                        f"manual unload is disabled: {self.spec.name}"
                    )
                if self._runtime.state in {
                    ModelReadinessState.DISABLED,
                    ModelReadinessState.UNLOADED,
                }:
                    return False
                self._runtime.state = ModelReadinessState.DRAINING
                self._runtime.accepting_calls = False
                deadline = self._clock() + max(0.0, timeout_s)
                while self._runtime.active_calls:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._runtime.last_error_code = "drain_timeout"
                        handle = self._handle
                        drain_timed_out = True
                        break
                    self._condition.wait(timeout=remaining)
                if not drain_timed_out:
                    self._runtime.state = ModelReadinessState.UNLOADING
                    handle = self._handle
            if drain_timed_out:
                cancel = getattr(handle, "cancel", None)
                if callable(cancel):
                    try:
                        cancel()
                    except Exception:
                        pass
                raise ModelDrainTimeoutError(
                    f"model drain timed out: {self.spec.name}"
                )
            try:
                if self.spec.unloader is not None:
                    self.spec.unloader(handle)
            except Exception:
                with self._condition:
                    self._runtime.state = ModelReadinessState.FAILED
                    self._runtime.failure_count += 1
                    self._runtime.last_error_code = "unload_failed"
                    self._condition.notify_all()
                raise
            with self._condition:
                self._handle = None
                self._runtime.state = ModelReadinessState.UNLOADED
                self._runtime.accepting_calls = False
                self._runtime.retry_after = None
                self._condition.notify_all()
            return True

    @contextmanager
    def lease(self) -> Iterator[Any]:
        with self._condition:
            now = self._clock()
            if self._runtime.retry_after is not None and now < self._runtime.retry_after:
                raise ModelUnavailableError(f"model circuit open: {self.spec.name}")
            if not self._runtime.accepting_calls or self._runtime.state not in {
                ModelReadinessState.READY,
                ModelReadinessState.DEGRADED,
            }:
                raise ModelUnavailableError(f"model not ready: {self.spec.name}")
            self._runtime.active_calls += 1
            handle = self._handle
        try:
            yield handle
        finally:
            with self._condition:
                self._runtime.active_calls -= 1
                self._runtime.last_used_at = self._clock()
                self._condition.notify_all()

    def record_success(self) -> None:
        with self._condition:
            self._runtime.success_count += 1
            if self._runtime.state == ModelReadinessState.DEGRADED:
                self._runtime.state = ModelReadinessState.READY
            self._runtime.last_error_code = None

    def record_failure(
        self,
        error_code: str,
        *,
        retry_after: float | None = None,
        increment: bool = True,
    ) -> None:
        with self._condition:
            if increment:
                self._runtime.failure_count += 1
            self._runtime.last_error_code = error_code
            if self._runtime.state == ModelReadinessState.READY:
                self._runtime.state = ModelReadinessState.DEGRADED
            self._runtime.retry_after = retry_after

    def snapshot(self) -> dict:
        with self._condition:
            runtime = self._runtime
            circuit_open = (
                runtime.retry_after is not None
                and self._clock() < runtime.retry_after
            )
            return {
                "runtime_state": runtime.state.value,
                "runtime_enabled": runtime.state != ModelReadinessState.DISABLED,
                "callable": (
                    runtime.state
                    in {ModelReadinessState.READY, ModelReadinessState.DEGRADED}
                    and runtime.accepting_calls
                    and not circuit_open
                ),
                "active_calls": runtime.active_calls,
                "accepting_calls": runtime.accepting_calls,
                "last_used_at": runtime.last_used_at,
                "loaded_at": runtime.loaded_at,
                "failure_count": runtime.failure_count,
                "success_count": runtime.success_count,
                "retry_after": runtime.retry_after,
                "last_error_code": runtime.last_error_code,
                "priority": self.spec.priority,
                "evictable": self.spec.evictable,
                "pinned": self.spec.pinned,
                "estimated_vram_mb": self.spec.estimated_vram_mb,
            }
