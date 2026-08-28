import threading
import time
from collections.abc import Callable

from yuki.cognition.brain.soul_reflector import SoulReflector
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.soul_scheduler")


class SoulReflectionScheduler:
    """Trigger one-at-a-time Soul reflections by utterance count or wall clock."""

    def __init__(
        self,
        reflector: SoulReflector,
        *,
        every_utterances: int = 30,
        interval_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reflector = reflector
        self._every_utterances = max(1, int(every_utterances))
        self._interval_s = max(0.01, float(interval_s))
        self._clock = clock
        self._condition = threading.Condition()
        self._started = False
        self._stop = False
        self._running = False
        self._pending = False
        self._utterances = 0
        self._next_due_at = self._clock() + self._interval_s
        self._timer_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._stop or (
                self._timer_thread is not None and self._timer_thread.is_alive()
            ):
                return
            self._started = True
            self._next_due_at = self._clock() + self._interval_s
            self._timer_thread = threading.Thread(
                target=self._timer_loop,
                daemon=True,
                name="yuki-soul-reflection-timer",
            )
            self._timer_thread.start()

    def on_utterance(self, _text: str = "") -> None:
        with self._condition:
            if self._stop or not self._started:
                return
            self._utterances += 1
            if self._utterances < self._every_utterances:
                return
            self._reset_deadlines_locked()
            self._schedule_locked()

    def close(self, timeout_s: float | None = None) -> None:
        with self._condition:
            self._stop = True
            self._started = False
            self._pending = False
            self._condition.notify_all()
            timer = self._timer_thread
            worker = self._worker_thread
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=timeout_s)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout_s)

    def _timer_loop(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
                delay = max(0.0, self._next_due_at - self._clock())
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue
                self._reset_deadlines_locked()
                self._schedule_locked()

    def _reset_deadlines_locked(self) -> None:
        self._utterances = 0
        self._next_due_at = self._clock() + self._interval_s
        self._condition.notify_all()

    def _schedule_locked(self) -> None:
        if self._stop or not self._started:
            return
        if self._running:
            self._pending = True
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._run_reflection,
            daemon=True,
            name="yuki-soul-reflection-worker",
        )
        self._worker_thread.start()

    def _run_reflection(self) -> None:
        try:
            self._reflector.reflect(cancelled=self._is_stopped)
        except Exception:
            logger.warning("soul reflection failed", exc_info=True)
        finally:
            with self._condition:
                self._running = False
                if self._pending and not self._stop:
                    self._pending = False
                    self._schedule_locked()
                else:
                    self._worker_thread = None
                self._condition.notify_all()

    def _is_stopped(self) -> bool:
        with self._condition:
            return self._stop
