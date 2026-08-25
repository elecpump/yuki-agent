from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelIncident:
    model: str
    kind: str
    message: str
    correlation_id: str
    ts: float

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "kind": self.kind,
            "message": self.message,
            "correlation_id": self.correlation_id,
            "ts": self.ts,
        }


class ModelErrorContext:
    """Keeps recent model failures with correlation ids for health/debug views."""

    def __init__(self, *, max_incidents: int = 100, clock=time.time) -> None:
        self._incidents: deque[ModelIncident] = deque(maxlen=max(1, int(max_incidents)))
        self._clock = clock

    def record(
        self,
        model: str,
        error: Exception | str,
        *,
        correlation_id: str | None = None,
        kind: str | None = None,
    ) -> dict:
        incident = ModelIncident(
            model=model,
            kind=kind or classify_model_error(error),
            message=str(error),
            correlation_id=correlation_id or uuid.uuid4().hex,
            ts=float(self._clock()),
        )
        self._incidents.append(incident)
        return incident.as_dict()

    def recent_incidents(self, *, limit: int | None = None) -> list[dict]:
        incidents = list(self._incidents)
        if limit is not None:
            incidents = incidents[-max(0, int(limit)) :]
        return [incident.as_dict() for incident in incidents]


def classify_model_error(error: Exception | str) -> str:
    message = str(error).lower()
    name = error.__class__.__name__.lower() if isinstance(error, Exception) else ""
    if "outofmemory" in name or "out of memory" in message or "cuda oom" in message:
        return "gpu_oom"
    if "timeout" in message:
        return "timeout"
    return "model_error"
