"""Lifecycle and orchestration for proactive cognition decisions."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from yuki.cognition.brain.classifier import Emotion
from yuki.cognition.brain.cooldown import CooldownCalculator
from yuki.cognition.brain.decision_contract import (
    DecisionTrace,
    final_reply_payload,
    situation_provenance,
)
from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot
from yuki.cognition.context.working import WorkingContext
from yuki.logger import get_logger
from yuki.runtime_bus import RuntimeBusProtocol
from yuki.topics import Topics

if TYPE_CHECKING:
    from yuki.cognition.brain.soul import SoulStore
    from yuki.cognition.l2.proactive import ProactiveAgent

logger = get_logger("yuki.cognition.brain.proactive_controller")
MAX_FINGERPRINT_STATES = 1024


class ProactiveTrigger(StrEnum):
    SITUATION = "situation"
    TICK = "tick"


class DecisionTraceLogger(Protocol):
    def info(self, event: str, **fields: object) -> object: ...


class ProactiveController:
    """Run hard gates, one cloud worker, backoff, and bounded tick lifecycle."""

    def __init__(
        self,
        bus: RuntimeBusProtocol,
        agent: ProactiveAgent | None,
        cooldown: CooldownCalculator,
        *,
        trace_logger: DecisionTraceLogger,
        context: WorkingContext | None = None,
        projector: ContextProjector | None = None,
        soul_store: SoulStore | None = None,
        enabled: bool = True,
        tick_s: float = 30.0,
        activity_suppress_s: float = 30.0,
        dedup_min_interval_s: float = 30.0,
        silent_hold_s: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bus = bus
        self._agent = agent
        self._cooldown = cooldown
        self._trace_logger = trace_logger
        self._context = context
        self._projector = projector
        self._soul_store = soul_store
        self._enabled = bool(enabled)
        self._tick_s = max(0.0, float(tick_s))
        self._activity_suppress_s = max(0.0, float(activity_suppress_s))
        self._dedup_min_interval_s = max(0.0, float(dedup_min_interval_s))
        self._silent_hold_s = max(0.0, float(silent_hold_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._pending_input_ts = 0.0
        self._last_utterance_ts: float | None = None
        self._running = False
        self._pending: tuple[ProactiveTrigger, dict | None] | None = None
        self._started = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._tick_worker: threading.Thread | None = None
        self._attempts: dict[tuple[str, str], float] = {}
        self._silent: dict[tuple[str, str], float] = {}
        self._failure_streak = 0
        self._disabled_until = 0.0

    @property
    def pending_input_ts(self) -> float:
        with self._probe_lock:
            return self._pending_input_ts

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            if self._tick_s <= 0.0:
                return
            self._tick_worker = threading.Thread(
                target=self._run_ticks, daemon=True, name="yuki-proactive-tick"
            )
            self._tick_worker.start()

    def close(self, timeout_s: float = 1.0) -> None:
        with self._lock:
            self._started = False
            self._pending = None
            self._stop.set()
            workers = (self._tick_worker, self._worker)
        deadline = time.monotonic() + max(0.0, timeout_s)
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))

    def on_user_utterance(self, text: str, ts: float) -> None:
        with self._probe_lock:
            self._last_utterance_ts = max(self._last_utterance_ts or 0.0, ts)
            self._pending_input_ts = max(self._pending_input_ts, ts)
        self._cooldown.on_user_utterance(text, ts)

    def on_input_probe(self, ts: float) -> None:
        with self._probe_lock:
            self._last_utterance_ts = max(self._last_utterance_ts or 0.0, ts)
            self._pending_input_ts = max(self._pending_input_ts, ts)

    def schedule_situation(self, situation: dict) -> None:
        self._schedule(ProactiveTrigger.SITUATION, situation)

    def trigger_tick(self, situation: dict | None) -> None:
        self._schedule(ProactiveTrigger.TICK, situation)

    def _run_ticks(self) -> None:
        while not self._stop.wait(self._tick_s):
            situation = self._context.situation() if self._context is not None else None
            self.trigger_tick(situation)

    def _schedule(self, trigger: ProactiveTrigger, situation: dict | None) -> None:
        now = self._clock()
        with self._lock:
            if not self._started or not self._passes_gates(situation, now):
                return
            self._pending = (trigger, dict(situation) if situation else None)
            if self._running:
                return
            self._running = True
            self._worker = threading.Thread(
                target=self._run_worker, daemon=True, name="yuki-proactive"
            )
            self._worker.start()

    def _passes_gates(self, situation: dict | None, now: float) -> bool:
        self._prune_fingerprints(now)
        if not self._enabled or self._agent is None or now < self._disabled_until:
            return False
        fingerprint = self._fingerprint(situation)
        attempted = self._attempts.get(fingerprint)
        if attempted is not None and now - attempted < self._dedup_min_interval_s:
            return False
        silent = self._silent.get(fingerprint)
        if silent is not None and now - silent < self._silent_hold_s:
            return False
        with self._probe_lock:
            utterance = self._last_utterance_ts
        if utterance is not None and now - utterance < self._activity_suppress_s:
            return False
        return self._cooldown.is_available(now)

    def _run_worker(self) -> None:
        while True:
            with self._lock:
                pending = self._pending
                self._pending = None
                if pending is None or not self._started:
                    self._running = False
                    self._worker = None
                    return
            self._evaluate(*pending)

    def _evaluate(self, trigger: ProactiveTrigger, situation: dict | None) -> None:
        started = self._clock()
        fingerprint = self._fingerprint(situation)
        with self._lock:
            if not self._started or not self._passes_gates(situation, started):
                return
            self._attempts[fingerprint] = started
            self._prune_fingerprints(started)
        snapshot = ContextSnapshot(situation=situation)
        if self._context is not None and self._projector is not None:
            snapshot = self._projector.build(self._context)
        soul = self._soul_store.load_or_default() if self._soul_store is not None else {}
        try:
            decision = self._agent.decide(snapshot, soul)
        except Exception:
            logger.warning("proactive decision failed", exc_info=True)
            self._finish("fail", fingerprint, trigger, situation, "cloud_error")
            return
        if decision.reason in {"cloud_error", "parse_error"}:
            outcome = "fail" if decision.reason == "cloud_error" else "parse_error"
            self._finish(outcome, fingerprint, trigger, situation, decision.reason)
            return
        self._failure_streak = 0
        if decision.action == "silent":
            self._finish("silent", fingerprint, trigger, situation, decision.reason)
            return
        with self._probe_lock:
            interrupted = self._pending_input_ts > started
        with self._lock:
            active = self._started
        if interrupted or not active:
            self._cooldown.defer_without_signal(self._clock())
            self._trace(trigger, "silent", "interrupted", situation)
            return
        reply_ts = self._clock()
        reply_id = uuid.uuid4().hex
        self._bus.publish(
            Topics.REPLY,
            final_reply_payload(decision.text, reply_ts, Emotion.NEUTRAL.value, reply_id),
        )
        if self._context is not None:
            self._context.add_agent(decision.text)
        self._cooldown.on_decision("speak", reply_ts)
        self._trace(trigger, "speak", decision.reason, situation, decision.text, reply_id)

    def _finish(
        self,
        outcome: str,
        fingerprint: tuple[str, str],
        trigger: ProactiveTrigger,
        situation: dict | None,
        reason: str,
    ) -> None:
        now = self._clock()
        self._cooldown.on_decision(outcome, now)
        if outcome in {"fail", "parse_error"}:
            self._failure_streak += 1
            if self._failure_streak >= 3:
                self._disabled_until = now + 60.0
        else:
            self._failure_streak = 0
        if outcome == "silent":
            self._silent[fingerprint] = now
            self._prune_fingerprints(now)
        self._trace(trigger, "silent", reason, situation)

    def _trace(
        self,
        trigger: ProactiveTrigger,
        action: str,
        reason: str,
        situation: dict | None,
        rendered: str = "",
        reply_id: str | None = None,
    ) -> None:
        now = self._clock()
        self._trace_logger.info(
            "decision",
            **DecisionTrace(
                ts=now,
                trigger=trigger.value,
                emotion=Emotion.NEUTRAL.value,
                actions=[action],
                rendered=rendered,
                reason=reason,
                route="proactive",
                reply_id=reply_id,
                cooldown_state=self._cooldown.snapshot(now),
                situation_provenance=situation_provenance(situation),
            ).to_dict(),
            llm_reason=reason,
        )

    @staticmethod
    def _fingerprint(situation: dict | None) -> tuple[str, str]:
        current = situation or {}
        return str(current.get("source_id", "")), str(current.get("topic", ""))

    def _prune_fingerprints(self, now: float) -> None:
        self._prune_mapping(
            self._attempts,
            cutoff=now - self._dedup_min_interval_s,
        )
        self._prune_mapping(
            self._silent,
            cutoff=now - self._silent_hold_s,
        )

    @staticmethod
    def _prune_mapping(mapping: dict[tuple[str, str], float], *, cutoff: float) -> None:
        expired = [fingerprint for fingerprint, ts in mapping.items() if ts <= cutoff]
        for fingerprint in expired:
            del mapping[fingerprint]
        while len(mapping) > MAX_FINGERPRINT_STATES:
            del mapping[min(mapping, key=mapping.__getitem__)]
