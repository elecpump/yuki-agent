from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Iterator

from yuki.cognition.error_context import ModelErrorContext
from yuki.cognition.gpu_monitor import GpuMemoryMonitor


class ModelState(str, Enum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    LOADED = "loaded"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    loader: Callable[[], Any]
    unloader: Callable[[Any], None] | None = None
    health_check: Callable[[], dict] | None = None
    priority: int = 100
    dependencies: list[str] = field(default_factory=list)
    vram_estimate_gb: float = 0.0
    allow_unload: bool = True
    critical: bool = False


@dataclass
class _ModelEntry:
    spec: ModelSpec
    state: ModelState = ModelState.NOT_LOADED
    handle: Any = None
    last_error: str = ""


@dataclass
class _ModelStats:
    success_count: int = 0
    failure_count: int = 0
    last_error: str = ""
    latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=256))


class ModelRegistry:
    """Central lifecycle and health registry for local model-like components."""

    def __init__(
        self,
        *,
        gpu_monitor: GpuMemoryMonitor | None = None,
        error_context: ModelErrorContext | None = None,
    ) -> None:
        self._entries: dict[str, _ModelEntry] = {}
        self._stats: dict[str, _ModelStats] = {}
        self._gpu_monitor = gpu_monitor
        self._error_context = error_context or ModelErrorContext()
        self._lock = RLock()

    def register(self, spec: ModelSpec) -> None:
        if not spec.name:
            raise ValueError("model name is required")
        with self._lock:
            if spec.name in self._entries:
                raise ValueError(f"model already registered: {spec.name}")
            missing = [dep for dep in spec.dependencies if dep not in self._entries]
            if missing:
                raise ValueError(f"unknown model dependencies for {spec.name}: {missing}")
            self._entries[spec.name] = _ModelEntry(spec=spec)
            self._stats[spec.name] = _ModelStats()

    def load(self, model_id: str) -> None:
        with self._lock:
            self._load_locked(model_id, visiting=set())

    def unload(self, model_id: str) -> None:
        with self._lock:
            self._unload_locked(model_id, visiting=set())

    def reload(self, model_id: str) -> None:
        with self._lock:
            self._unload_locked(model_id, visiting=set())
            self._load_locked(model_id, visiting=set())

    @contextmanager
    def track_call(self, model_id: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        except Exception as exc:
            self.record_failure(model_id, exc, latency_ms=(time.perf_counter() - start) * 1000)
            raise
        else:
            self.record_success(model_id, latency_ms=(time.perf_counter() - start) * 1000)

    def record_success(self, model_id: str, *, latency_ms: float | None = None) -> None:
        with self._lock:
            stats = self._model_stats(model_id)
            stats.success_count += 1
            if latency_ms is not None:
                stats.latency_ms.append(max(0.0, float(latency_ms)))

    def record_failure(
        self,
        model_id: str,
        error: Exception | str,
        *,
        latency_ms: float | None = None,
    ) -> None:
        with self._lock:
            entry = self._entry(model_id)
            stats = self._model_stats(model_id)
            message = str(error)
            stats.failure_count += 1
            stats.last_error = message
            entry.last_error = message
            self._error_context.record(model_id, error)
            if latency_ms is not None:
                stats.latency_ms.append(max(0.0, float(latency_ms)))

    def get_loaded_models(self) -> list[str]:
        with self._lock:
            return [
                name
                for name, entry in self._entries.items()
                if entry.state == ModelState.LOADED or self._safe_health(entry).get("loaded", False)
            ]

    def get_model_health(self, model_id: str) -> dict:
        with self._lock:
            entry = self._entry(model_id)
            detail = self._safe_health(entry)
            state = self._state_from_health(entry, detail)
            return {
                "state": state.value,
                "loaded": bool(detail.get("loaded", state == ModelState.LOADED)),
                "degraded": bool(detail.get("degraded", state in {ModelState.DEGRADED, ModelState.ERROR})),
                "critical": entry.spec.critical,
                "priority": entry.spec.priority,
                "dependencies": list(entry.spec.dependencies),
                "vram_estimate_gb": entry.spec.vram_estimate_gb,
                "allow_unload": entry.spec.allow_unload,
                "last_error": entry.last_error,
                **self._stats_health(model_id),
                **detail,
            }

    def get_all_models_health(self) -> dict:
        with self._lock:
            return {name: self.get_model_health(name) for name in self._entries}

    def get_overall_status(self) -> dict:
        health = self.get_all_models_health()
        critical_bad = [
            name
            for name, detail in health.items()
            if detail.get("critical") and detail.get("degraded")
        ]
        any_degraded = any(detail.get("degraded") for detail in health.values())
        if critical_bad:
            status = "unhealthy"
        elif any_degraded:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "healthy": status != "unhealthy",
            "models": health,
            "gpu": self.gpu_health(),
            "recent_incidents": self._error_context.recent_incidents(limit=10),
        }

    def gpu_health(self) -> dict:
        if self._gpu_monitor is None:
            return {"available": False, "reason": "not_configured", "low_memory": False}
        return self._gpu_monitor.snapshot()

    def relieve_memory_pressure(self) -> dict:
        with self._lock:
            before = self.gpu_health()
            if not before.get("available", False):
                return {"action": "none", "reason": before.get("reason", "gpu_unavailable"), "gpu": before}
            if not before.get("low_memory", False):
                return {"action": "none", "reason": "memory_ok", "gpu": before}
            candidate = self._memory_pressure_candidate()
            if candidate is None:
                cleaned = self._gpu_monitor.empty_cache() if self._gpu_monitor is not None else False
                return {
                    "action": "none",
                    "reason": "no_unload_candidate",
                    "cache_cleared": cleaned,
                    "gpu": before,
                }
            self._unload_locked(candidate, visiting=set())
            cleaned = self._gpu_monitor.empty_cache() if self._gpu_monitor is not None else False
            return {
                "action": "unloaded",
                "model": candidate,
                "cache_cleared": cleaned,
                "gpu_before": before,
                "gpu_after": self.gpu_health(),
            }

    def shutdown(self) -> None:
        with self._lock:
            for name in self._unload_order():
                self._unload_locked(name, visiting=set())

    def _load_locked(self, model_id: str, *, visiting: set[str]) -> None:
        entry = self._entry(model_id)
        if entry.state == ModelState.LOADED:
            return
        if model_id in visiting:
            raise ValueError(f"cyclic model dependency at {model_id}")
        visiting.add(model_id)
        for dependency in entry.spec.dependencies:
            self._load_locked(dependency, visiting=visiting)
        visiting.remove(model_id)

        entry.state = ModelState.LOADING
        entry.last_error = ""
        try:
            entry.handle = entry.spec.loader()
            entry.state = ModelState.LOADED
        except Exception as exc:
            entry.state = ModelState.ERROR
            entry.last_error = str(exc)
            self.record_failure(model_id, exc)
            raise

    def _unload_locked(self, model_id: str, *, visiting: set[str]) -> None:
        entry = self._entry(model_id)
        if model_id in visiting:
            raise ValueError(f"cyclic model dependency at {model_id}")
        visiting.add(model_id)
        for dependent in self._dependents_of(model_id):
            self._unload_locked(dependent, visiting=visiting)
        visiting.remove(model_id)

        if not entry.spec.allow_unload:
            return
        health = self._safe_health(entry)
        if entry.state == ModelState.NOT_LOADED and not health.get("loaded", False):
            return
        try:
            if entry.spec.unloader is not None:
                entry.spec.unloader(entry.handle)
            entry.handle = None
            entry.state = ModelState.NOT_LOADED
            entry.last_error = ""
        except Exception as exc:
            entry.state = ModelState.ERROR
            entry.last_error = str(exc)
            self.record_failure(model_id, exc)
            raise

    def _entry(self, model_id: str) -> _ModelEntry:
        try:
            return self._entries[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model_id}") from exc

    def _model_stats(self, model_id: str) -> _ModelStats:
        self._entry(model_id)
        return self._stats.setdefault(model_id, _ModelStats())

    def _stats_health(self, model_id: str) -> dict:
        stats = self._model_stats(model_id)
        latencies = list(stats.latency_ms)
        last_error = stats.last_error or self._entries[model_id].last_error
        return {
            "latency_p50_ms": _percentile(latencies, 50.0),
            "latency_p95_ms": _percentile(latencies, 95.0),
            "success_count": stats.success_count,
            "failure_count": stats.failure_count,
            "last_error": last_error,
        }

    def _safe_health(self, entry: _ModelEntry) -> dict:
        if entry.spec.health_check is None:
            return {"loaded": entry.state == ModelState.LOADED}
        try:
            return dict(entry.spec.health_check())
        except Exception as exc:
            entry.state = ModelState.ERROR
            entry.last_error = str(exc)
            return {"loaded": False, "degraded": True, "error": "health_check_failed"}

    def _state_from_health(self, entry: _ModelEntry, detail: dict) -> ModelState:
        if entry.state == ModelState.ERROR:
            return ModelState.ERROR
        if bool(detail.get("degraded", False)):
            return ModelState.DEGRADED
        if bool(detail.get("loaded", False)):
            return ModelState.LOADED
        return entry.state

    def _dependents_of(self, model_id: str) -> list[str]:
        dependents = [
            name
            for name, entry in self._entries.items()
            if model_id in entry.spec.dependencies
        ]
        return sorted(dependents, key=lambda name: self._entries[name].spec.priority, reverse=True)

    def _unload_order(self) -> list[str]:
        return sorted(self._entries, key=lambda name: self._entries[name].spec.priority, reverse=True)

    def _memory_pressure_candidate(self) -> str | None:
        candidates = []
        for name, entry in self._entries.items():
            if not entry.spec.allow_unload:
                continue
            health = self._safe_health(entry)
            if entry.state == ModelState.LOADED or health.get("loaded", False):
                candidates.append(name)
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda name: (
                self._entries[name].spec.priority,
                self._entries[name].spec.vram_estimate_gb,
            ),
            reverse=True,
        )[0]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0)
    return round(float(ordered[int(index)]), 3)
