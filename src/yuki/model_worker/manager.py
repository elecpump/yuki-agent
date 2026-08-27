from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from yuki.model_worker.controller import (
    ManagedModelSpec,
    ModelController,
    ModelReadinessState,
)


_LEGACY_STATES = {
    ModelReadinessState.DISABLED: "not_loaded",
    ModelReadinessState.UNLOADED: "not_loaded",
    ModelReadinessState.LOADING: "loading",
    ModelReadinessState.READY: "loaded",
    ModelReadinessState.DEGRADED: "degraded",
    ModelReadinessState.FAILED: "error",
    ModelReadinessState.DRAINING: "loaded",
    ModelReadinessState.UNLOADING: "loaded",
}


class GpuMonitorProtocol(Protocol):
    def snapshot(self) -> dict: ...

    def empty_cache(self) -> bool: ...


class ModelManager:
    def __init__(
        self,
        *,
        gpu_monitor: GpuMonitorProtocol | None = None,
        drain_timeout_s: float = 10.0,
        circuit_breaker_s: float = 30.0,
        vram_safety_margin_mb: int = 512,
        vram_hysteresis_mb: int = 256,
        maintenance_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gpu_monitor = gpu_monitor
        self._drain_timeout_s = drain_timeout_s
        self._circuit_breaker_s = circuit_breaker_s
        self._vram_safety_margin_mb = vram_safety_margin_mb
        self._vram_hysteresis_mb = vram_hysteresis_mb
        self._maintenance_interval_s = max(0.05, maintenance_interval_s)
        self._clock = clock
        self._controllers: dict[str, ModelController] = {}
        self._management_lock = threading.RLock()
        self._latencies: dict[str, list[float]] = {}
        self._tracking = threading.local()
        self._cache_lock = threading.Lock()
        self._gpu_cache = {
            "available": False,
            "reason": "not_configured" if gpu_monitor is None else "not_sampled",
            "low_memory": False,
        }
        self._model_health_cache: dict[str, dict] = {}
        self._last_refresh_at: float | None = None
        self._worker_fatal = False
        self._worker_fatal_code: str | None = None
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    def register(self, spec: ManagedModelSpec) -> None:
        with self._management_lock:
            if spec.name in self._controllers:
                raise ValueError(f"model already registered: {spec.name}")
            missing = [name for name in spec.dependencies if name not in self._controllers]
            if missing:
                raise ValueError(f"unknown model dependencies for {spec.name}: {missing}")
            self._controllers[spec.name] = ModelController(spec, clock=self._clock)
            self._validate_acyclic()

    def names(self) -> list[str]:
        with self._management_lock:
            return list(self._controllers)

    def load(self, model: str) -> Any:
        with self._management_lock:
            required = self._dependency_closure(model)
            for name in required:
                controller = self._controller(name)
                if controller.snapshot()["runtime_state"] not in {"ready", "degraded"}:
                    self._admit(name, exclude=set(required))
                controller.load()
            return self._controller(model).handle

    def unload(self, model: str, *, manual: bool = True) -> None:
        with self._management_lock:
            for name in self._dependent_closure(model):
                self._controller(name).unload(
                    timeout_s=self._drain_timeout_s,
                    force_manual=manual,
                )

    def reload(self, model: str) -> Any:
        with self._management_lock:
            self.unload(model, manual=True)
            return self.load(model)

    @contextmanager
    def lease(self, model: str) -> Iterator[Any]:
        controller = self._controller(model)
        with controller.lease() as handle:
            yield handle

    @contextmanager
    def track_call(self, model: str) -> Iterator[None]:
        active = set(getattr(self._tracking, "models", ()))
        if model in active:
            yield
            return
        active.add(model)
        self._tracking.models = active
        started = time.perf_counter()
        try:
            yield
        except Exception as exc:
            self.record_failure(model, exc)
            raise
        else:
            self.record_success(model)
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            with self._management_lock:
                values = self._latencies.setdefault(model, [])
                values.append(elapsed)
                if len(values) > 256:
                    del values[:-256]
            active.remove(model)
            self._tracking.models = active

    def record_success(self, model: str, *, latency_ms: float | None = None) -> None:
        self._controller(model).record_success()
        if latency_ms is not None:
            with self._management_lock:
                self._latencies.setdefault(model, []).append(latency_ms)

    def record_failure(
        self,
        model: str,
        error: Exception | str,
        *,
        latency_ms: float | None = None,
    ) -> None:
        error_code = str(error) if isinstance(error, str) else "inference_failed"
        self._controller(model).record_failure(
            error_code,
            retry_after=None,
        )
        if latency_ms is not None:
            with self._management_lock:
                self._latencies.setdefault(model, []).append(latency_ms)

    def get_loaded_models(self) -> list[str]:
        return [
            name
            for name, detail in self.get_all_models_health().items()
            if detail["loaded"]
        ]

    def get_model_health(self, model: str) -> dict:
        controller = self._controller(model)
        detail = controller.snapshot()
        runtime_state = ModelReadinessState(detail["runtime_state"])
        latencies = list(self._latencies.get(model, ()))
        with self._cache_lock:
            runtime_health = dict(self._model_health_cache.get(model, {}))
        degraded = runtime_state in {
            ModelReadinessState.DEGRADED,
            ModelReadinessState.FAILED,
        } or bool(runtime_health.get("degraded", False))
        return {
            "state": _LEGACY_STATES[runtime_state],
            "loaded": runtime_state
            in {
                ModelReadinessState.READY,
                ModelReadinessState.DEGRADED,
                ModelReadinessState.DRAINING,
                ModelReadinessState.UNLOADING,
            },
            "degraded": degraded,
            "critical": False,
            "dependencies": list(controller.spec.dependencies),
            "vram_estimate_gb": controller.spec.estimated_vram_mb / 1024.0,
            "allow_unload": controller.spec.manual_unload_allowed,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "last_error": detail["last_error_code"] or "",
            "runtime_health": runtime_health,
            **detail,
        }

    def get_all_models_health(self) -> dict:
        return {name: self.get_model_health(name) for name in self.names()}

    def get_overall_status(self) -> dict:
        models = self.get_all_models_health()
        degraded = any(item["degraded"] for item in models.values())
        with self._cache_lock:
            worker_fatal = self._worker_fatal
            worker_fatal_code = self._worker_fatal_code
        return {
            "status": "unhealthy" if worker_fatal else "degraded" if degraded else "healthy",
            "healthy": not worker_fatal,
            "worker_fatal": worker_fatal,
            "worker_fatal_code": worker_fatal_code,
            "models": models,
            "gpu": self.gpu_health(),
            "recent_incidents": [],
        }

    def preflight(self, model: str | None = None) -> dict:
        names = [model] if model is not None else self.names()
        results = {}
        for name in names:
            controller = self._controller(name)
            checks = []
            if controller.spec.preflight_check is not None:
                try:
                    custom = dict(controller.spec.preflight_check())
                except Exception:
                    custom = {"ok": False, "reason": "preflight_failed"}
                checks.append(custom)
            estimate = controller.spec.estimated_vram_mb
            free = self._free_vram_mb()
            checks.append(
                {
                    "name": "vram_estimate",
                    "ok": free is None or free >= estimate + self._vram_safety_margin_mb,
                    "severity": "error",
                    "detail": {"estimated_vram_mb": estimate, "free_vram_mb": free},
                }
            )
            ok = all(check.get("ok", True) for check in checks)
            results[name] = {"ok": ok, "status": "ok" if ok else "failed", "checks": checks}
        ok = all(item["ok"] for item in results.values())
        return {
            "ok": ok,
            "status": "ok" if ok else "failed",
            "models": results,
            "gpu": self.gpu_health(),
        }

    def relieve_memory_pressure(self) -> dict:
        with self._management_lock:
            before = self._sample_gpu_health()
            candidate = self._eviction_candidate(exclude=set(), emergency=True)
            if candidate is None:
                cleaned = bool(
                    self._gpu_monitor is not None
                    and getattr(self._gpu_monitor, "empty_cache", lambda: False)()
                )
                return {
                    "action": "none",
                    "reason": "no_unload_candidate",
                    "cache_cleared": cleaned,
                    "gpu": before,
                }
            self._controller(candidate).unload(timeout_s=self._drain_timeout_s)
            return {
                "action": "unloaded",
                "model": candidate,
                "models": [candidate],
                "gpu_before": before,
                "gpu_after": self._sample_gpu_health(),
            }

    def gpu_health(self) -> dict:
        with self._cache_lock:
            return dict(self._gpu_cache)

    def start_maintenance(self) -> None:
        if self._maintenance_thread is not None and self._maintenance_thread.is_alive():
            return
        self._maintenance_stop.clear()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            daemon=True,
            name="yuki-model-manager",
        )
        self._maintenance_thread.start()

    def stop_maintenance(self) -> None:
        self._maintenance_stop.set()
        thread = self._maintenance_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self._maintenance_interval_s * 2))
        self._maintenance_thread = None

    def maintenance_snapshot(self) -> dict:
        thread = self._maintenance_thread
        with self._cache_lock:
            last_refresh_at = self._last_refresh_at
        return {
            "healthy": thread is not None and thread.is_alive(),
            "last_refresh_at": last_refresh_at,
            "interval_s": self._maintenance_interval_s,
        }

    def run_maintenance_once(self) -> list[str]:
        self._refresh_health_cache()
        return self._reap_idle_models()

    def run_inference(
        self,
        model: str,
        callback: Callable[[Any], Any],
        *,
        oom_retry: int = 0,
    ) -> Any:
        attempts = 0
        while True:
            try:
                self.load(model)
                with self.lease(model) as handle:
                    with self.track_call(model):
                        return callback(handle)
            except Exception as exc:
                classification = self.classify_inference_error(exc)
                if classification == "worker_fatal":
                    self._mark_worker_fatal("cuda_context_failed")
                    self._controller(model).record_failure(
                        "worker_fatal",
                        increment=False,
                    )
                    raise
                if classification != "cuda_oom":
                    raise
                self._recover_oom(model)
                if attempts < max(0, oom_retry):
                    attempts += 1
                    continue
                self._controller(model).record_failure(
                    "cuda_oom",
                    retry_after=self._clock() + self._circuit_breaker_s,
                    increment=False,
                )
                raise

    @staticmethod
    def classify_inference_error(error: Exception) -> str:
        name = type(error).__name__.lower()
        message = str(error).lower()
        if "outofmemory" in name or "out of memory" in message and "cuda" in message:
            return "cuda_oom"
        fatal_markers = (
            "illegal memory access",
            "device-side assert",
            "device lost",
            "unspecified launch failure",
            "failed to synchronize",
            "cannot synchronize",
        )
        if any(marker in message for marker in fatal_markers):
            return "worker_fatal"
        return "inference_failed"

    def shutdown(self) -> None:
        self.stop_maintenance()
        with self._management_lock:
            for name in reversed(self.names()):
                try:
                    self._controller(name).unload(timeout_s=self._drain_timeout_s)
                except Exception:
                    continue

    def _admit(self, model: str, *, exclude: set[str]) -> None:
        estimate = self._controller(model).spec.estimated_vram_mb
        free = self._free_vram_mb()
        if estimate <= 0 or free is None:
            return
        target = estimate + self._vram_safety_margin_mb + self._vram_hysteresis_mb
        while free < target:
            candidate = self._eviction_candidate(exclude=exclude)
            if candidate is None:
                raise RuntimeError("insufficient_vram")
            self._controller(candidate).unload(timeout_s=self._drain_timeout_s)
            free = self._free_vram_mb()
            if free is None:
                return

    def _eviction_candidate(
        self,
        *,
        exclude: set[str],
        emergency: bool = False,
    ) -> str | None:
        now = self._clock()
        candidates = []
        for name, controller in self._controllers.items():
            if name in exclude:
                continue
            detail = controller.snapshot()
            if detail["runtime_state"] not in {"ready", "degraded"}:
                continue
            if not controller.spec.evictable or controller.spec.pinned:
                continue
            if detail["active_calls"]:
                continue
            loaded_at = detail["loaded_at"] or 0.0
            if not emergency and now - loaded_at < controller.spec.min_residency_s:
                continue
            candidates.append((controller.spec.priority, detail["last_used_at"] or 0.0, name))
        return min(candidates)[2] if candidates else None

    def _free_vram_mb(self) -> int | None:
        gpu = self._sample_gpu_health()
        if not gpu.get("available", False):
            return None
        if "free_mb" in gpu:
            return int(gpu["free_mb"])
        if "free_gb" in gpu:
            return int(float(gpu["free_gb"]) * 1024)
        return None

    def _sample_gpu_health(self) -> dict:
        if self._gpu_monitor is None:
            return {"available": False, "reason": "not_configured", "low_memory": False}
        try:
            return dict(self._gpu_monitor.snapshot())
        except Exception:
            return {"available": False, "reason": "snapshot_failed", "low_memory": False}

    def _refresh_health_cache(self) -> None:
        gpu = self._sample_gpu_health()
        with self._management_lock:
            controllers = list(self._controllers.items())
        model_health: dict[str, dict] = {}
        for name, controller in controllers:
            callback = controller.spec.health_check
            if callback is None:
                model_health[name] = {}
                continue
            try:
                model_health[name] = dict(callback())
            except Exception:
                model_health[name] = {"degraded": True, "reason": "health_check_failed"}
        with self._cache_lock:
            self._gpu_cache = gpu
            self._model_health_cache = model_health
            self._last_refresh_at = self._clock()

    def _reap_idle_models(self) -> list[str]:
        now = self._clock()
        reaped: list[str] = []
        with self._management_lock:
            for name, controller in self._controllers.items():
                idle_s = controller.spec.idle_unload_s
                detail = controller.snapshot()
                if idle_s <= 0 or detail["runtime_state"] not in {"ready", "degraded"}:
                    continue
                if not controller.spec.evictable or controller.spec.pinned:
                    continue
                if detail["active_calls"]:
                    continue
                loaded_at = detail["loaded_at"]
                if loaded_at is None:
                    loaded_at = now
                last_used_at = detail["last_used_at"]
                if last_used_at is None:
                    last_used_at = loaded_at
                if now - loaded_at < controller.spec.min_residency_s:
                    continue
                if now - last_used_at < idle_s:
                    continue
                try:
                    controller.unload(timeout_s=self._drain_timeout_s)
                except Exception:
                    continue
                reaped.append(name)
        return reaped

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.is_set():
            try:
                self.run_maintenance_once()
            except Exception:
                pass
            if self._maintenance_stop.wait(self._maintenance_interval_s):
                return

    def _recover_oom(self, model: str) -> None:
        with self._management_lock:
            if self._gpu_monitor is not None:
                self._gpu_monitor.empty_cache()
            candidate = self._eviction_candidate(exclude={model}, emergency=True)
            if candidate is not None:
                self._controller(candidate).unload(timeout_s=self._drain_timeout_s)

    def _mark_worker_fatal(self, error_code: str) -> None:
        with self._cache_lock:
            self._worker_fatal = True
            self._worker_fatal_code = error_code

    def _controller(self, model: str) -> ModelController:
        try:
            return self._controllers[model]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model}") from exc

    def _dependency_closure(self, model: str) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            for dependency in self._controller(name).spec.dependencies:
                visit(dependency)
            ordered.append(name)

        visit(model)
        return ordered

    def _dependent_closure(self, model: str) -> list[str]:
        ordered = []

        def visit(name: str) -> None:
            for candidate, controller in self._controllers.items():
                if name in controller.spec.dependencies:
                    visit(candidate)
            if name not in ordered:
                ordered.append(name)

        visit(model)
        return ordered

    def _validate_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"cyclic model dependency at {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in self._controller(name).spec.dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in self._controllers:
            visit(name)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100.0)
    return round(float(ordered[index]), 3)
