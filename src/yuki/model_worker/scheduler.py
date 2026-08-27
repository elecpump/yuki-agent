from __future__ import annotations

import itertools
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any


class SchedulerBusyError(RuntimeError):
    pass


@dataclass(order=True)
class _Task:
    sort_key: tuple[int, int]
    fn: Callable[[], Any] = field(compare=False)
    future: Future = field(compare=False)
    enqueued_at: float = field(compare=False)


class ModelInferenceScheduler:
    def __init__(
        self,
        *,
        concurrency: int = 1,
        interactive_queue_size: int = 32,
        background_queue_size: int = 16,
    ) -> None:
        self._interactive: queue.PriorityQueue = queue.PriorityQueue(
            maxsize=max(1, interactive_queue_size)
        )
        self._background: queue.PriorityQueue = queue.PriorityQueue(
            maxsize=max(1, background_queue_size)
        )
        self._sequence = itertools.count()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._queue_wait_ms: list[float] = []
        self._threads = [
            threading.Thread(
                target=self._run,
                daemon=True,
                name=f"yuki-model-scheduler:{index}",
            )
            for index in range(max(1, concurrency))
        ]
        for thread in self._threads:
            thread.start()

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        lane: str = "interactive",
        priority: int = 50,
    ) -> Future:
        if self._stop.is_set():
            raise SchedulerBusyError("scheduler_stopped")
        target = self._interactive if lane == "interactive" else self._background
        if lane not in {"interactive", "background"}:
            raise ValueError(f"unknown scheduler lane: {lane}")
        future: Future = Future()
        task = _Task(
            sort_key=(-priority, next(self._sequence)),
            fn=fn,
            future=future,
            enqueued_at=time.monotonic(),
        )
        try:
            target.put_nowait(task)
        except queue.Full as exc:
            raise SchedulerBusyError(f"{lane}_queue_full") from exc
        return future

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            waits = list(self._queue_wait_ms)
        interactive_oldest = self._oldest_wait_ms(self._interactive, now)
        background_oldest = self._oldest_wait_ms(self._background, now)
        return {
            "interactive_depth": self._interactive.qsize(),
            "background_depth": self._background.qsize(),
            "interactive_oldest_wait_ms": interactive_oldest,
            "background_oldest_wait_ms": background_oldest,
            "queue_wait_p95_ms": _percentile(waits, 95),
            "healthy": not self._stop.is_set() and all(t.is_alive() for t in self._threads),
        }

    @staticmethod
    def _oldest_wait_ms(target: queue.PriorityQueue, now: float) -> float:
        with target.mutex:
            queued = list(target.queue)
        if not queued:
            return 0.0
        return round(max(0.0, (now - min(task.enqueued_at for task in queued)) * 1000.0), 3)

    def close(self) -> None:
        self._stop.set()
        for target in (self._interactive, self._background):
            while True:
                try:
                    task = target.get_nowait()
                except queue.Empty:
                    break
                task.future.cancel()
        for thread in self._threads:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._interactive.get_nowait()
            except queue.Empty:
                try:
                    task = self._background.get(timeout=0.05)
                except queue.Empty:
                    continue
            if not task.future.set_running_or_notify_cancel():
                continue
            wait_ms = (time.monotonic() - task.enqueued_at) * 1000.0
            with self._lock:
                self._queue_wait_ms.append(wait_ms)
                if len(self._queue_wait_ms) > 256:
                    del self._queue_wait_ms[:-256]
            try:
                result = task.fn()
            except BaseException as exc:
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100.0)
    return round(float(ordered[index]), 3)
