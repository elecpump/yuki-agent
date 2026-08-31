"""Adaptive cooldown state for proactive decisions."""

import json
import threading
import time
from collections import deque
from pathlib import Path

from yuki.logger import get_logger
from yuki.persistence import atomic_write_json

logger = get_logger("yuki.cognition.brain.cooldown")

DEFAULT_COOLDOWN_S = 120.0
DEFAULT_FLOOR_S = 30.0
DEFAULT_SILENT_STREAK = 0
SILENT_STREAK_CAP = 3
QUIET_BREAK_S = 600.0
ACTIVE_COOLDOWN_S = 300.0
QUIET_COOLDOWN_S = 60.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class CooldownCalculator:
    """Combine recent activity, model silence, and explicit feedback."""

    def __init__(
        self,
        initial_s: float = DEFAULT_COOLDOWN_S,
        *,
        path: str | Path | None = None,
        persona_name: str = "yuki",
        window_s: float = 300.0,
        max_cooldown_s: float = 600.0,
        floor_step_s: float = 30.0,
        legacy_path: str | Path | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._persona_name = persona_name
        self._window_s = max(1.0, float(window_s))
        self._max_s = max(0.0, float(max_cooldown_s))
        self._floor_step_s = max(0.0, float(floor_step_s))
        self._floor_s = min(DEFAULT_FLOOR_S, self._max_s)
        self._cooldown_s = min(max(float(initial_s), self._floor_s), self._max_s)
        self._silent_streak = DEFAULT_SILENT_STREAK
        self._negative_streak = 0
        self._utterance_timestamps: deque[float] = deque()
        self._last_utterance: float | None = None
        self._next_available_ts = 0.0
        self._lock = threading.RLock()
        self._restore()

    @property
    def cooldown_s(self) -> float:
        with self._lock:
            return self._cooldown_s

    @property
    def floor_s(self) -> float:
        with self._lock:
            return self._floor_s

    @property
    def silent_streak(self) -> int:
        with self._lock:
            return self._silent_streak

    @property
    def next_available_ts(self) -> float:
        with self._lock:
            return self._next_available_ts

    def last_utterance_ts(self) -> float | None:
        with self._lock:
            return self._last_utterance

    def on_user_utterance(self, ts: float) -> None:
        with self._lock:
            timestamp = float(ts)
            self._utterance_timestamps.append(timestamp)
            self._last_utterance = timestamp
            self._prune(timestamp)

    def apply_polarity(self, polarity: str, ts: float) -> None:
        """Adjust cooldown from model-judged feedback polarity (negative/positive/neutral)."""
        with self._lock:
            if polarity == "negative":
                self._negative_streak += 1
                self._cooldown_s = min(
                    max(self._cooldown_s * 1.5, self._floor_s + self._floor_step_s),
                    self._max_s,
                )
                if self._negative_streak >= 3:
                    self._negative_streak = 0
                    self._floor_s = min(self._floor_s + self._floor_step_s, self._max_s)
                    self._cooldown_s = max(self._cooldown_s, self._floor_s)
                self._persist()
            elif polarity == "positive":
                self._negative_streak = 0
                self._cooldown_s = max(self._cooldown_s * 0.8, self._floor_s)
                self._persist()
            # neutral: no state change

    def base_cooldown(self, now: float) -> float:
        with self._lock:
            current = float(now)
            self._prune(current)
            if (
                self._last_utterance is not None
                and current - self._last_utterance > QUIET_BREAK_S
            ):
                return min(QUIET_COOLDOWN_S, self._max_s)
            count = len(self._utterance_timestamps)
            base = ACTIVE_COOLDOWN_S if count >= 4 else DEFAULT_COOLDOWN_S
            return min(base, self._max_s)

    def on_decision(self, outcome: str, now: float) -> None:
        if outcome not in {"speak", "silent", "fail", "parse_error"}:
            raise ValueError(f"unknown proactive outcome: {outcome}")
        with self._lock:
            base = self._effective_cooldown(now)
            if outcome == "speak":
                delay = base
                self._silent_streak = DEFAULT_SILENT_STREAK
            elif outcome == "silent":
                delay = min(
                    base * (1.5 ** min(self._silent_streak, SILENT_STREAK_CAP)),
                    self._max_s,
                )
                self._silent_streak = min(self._silent_streak + 1, SILENT_STREAK_CAP)
            elif outcome == "fail":
                delay = min(base * 2.0, self._max_s)
            else:
                delay = min(base * 1.5, self._max_s)
            self._next_available_ts = float(now) + delay
            self._persist()

    def is_available(self, now: float) -> bool:
        with self._lock:
            return float(now) >= self._next_available_ts

    def defer_without_signal(self, now: float) -> None:
        """Apply a base delay without treating superseded work as a model decision."""
        with self._lock:
            self._next_available_ts = float(now) + self._effective_cooldown(now)
            self._persist()

    def snapshot(self, now: float) -> dict:
        with self._lock:
            return {
                "base_s": self.base_cooldown(now),
                "effective_s": self._effective_cooldown(now),
                "cooldown_s": self._cooldown_s,
                "floor_s": self._floor_s,
                "silent_streak": self._silent_streak,
                "next_available_ts": self._next_available_ts,
            }

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._utterance_timestamps and self._utterance_timestamps[0] < cutoff:
            self._utterance_timestamps.popleft()

    def _effective_cooldown(self, now: float) -> float:
        base = self.base_cooldown(now)
        adjusted = base * (self._cooldown_s / DEFAULT_COOLDOWN_S)
        return min(max(adjusted, self._floor_s), self._max_s)

    def _restore(self) -> None:
        source = self._path if self._path is not None and self._path.exists() else None
        migrating = False
        if source is None and self._legacy_path is not None and self._legacy_path.exists():
            source = self._legacy_path
            migrating = True
        if source is None:
            return
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("cooldown state read failed", error=str(exc))
            return
        if (
            not isinstance(data, dict)
            or data.get("persona_name", self._persona_name) != self._persona_name
        ):
            return
        cooldown = data.get("proactive_cooldown_s" if migrating else "cooldown_s")
        floor = data.get("cooldown_floor_s" if migrating else "floor_s")
        if isinstance(floor, (int, float)) and not isinstance(floor, bool):
            self._floor_s = min(max(float(floor), 0.0), self._max_s)
        if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool):
            self._cooldown_s = min(max(float(cooldown), self._floor_s), self._max_s)
        streak = data.get("silent_streak", DEFAULT_SILENT_STREAK)
        if isinstance(streak, int) and not isinstance(streak, bool):
            self._silent_streak = min(max(0, streak), SILENT_STREAK_CAP)
        if migrating:
            self._persist()

    def _persist(self) -> None:
        if self._path is None:
            return
        payload = {
            "persona_name": self._persona_name,
            "cooldown_s": self._cooldown_s,
            "floor_s": self._floor_s,
            "silent_streak": self._silent_streak,
            "updated_at": _now_iso(),
        }
        try:
            atomic_write_json(self._path, payload)
        except OSError as exc:
            logger.warning("cooldown state write failed", error=str(exc))
