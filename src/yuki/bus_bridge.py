from __future__ import annotations

import base64
import queue
import threading
from typing import Any

import numpy as np

from yuki.runtime_bus import (
    EventHandler,
    LocalRuntimeBus,
    RuntimeBusProtocol,
    ServiceHandler,
)
from yuki.topics import Topics


class WireCodec:
    """Translate native in-process payloads at the protobuf Struct boundary."""

    def encode_event(self, topic: str, payload: Any) -> dict:
        data = dict(payload or {})
        if topic == Topics.MIC and "samples" in data:
            samples = np.asarray(data.pop("samples"), dtype=np.float32)
            data["pcm"] = base64.b64encode(samples.tobytes()).decode("ascii")
        return data

    def decode_event(self, topic: str, payload: dict) -> dict:
        data = dict(payload or {})
        if topic == Topics.MIC and "samples" not in data and data.get("pcm"):
            data["samples"] = np.frombuffer(
                base64.b64decode(data.pop("pcm")),
                dtype=np.float32,
            ).copy()
            data["samples"].setflags(write=False)
        return data

    def encode_service_request(self, service: str, payload: dict) -> dict:
        del service
        return dict(payload or {})

    def decode_service_request(self, service: str, payload: dict) -> dict:
        del service
        return dict(payload or {})

    def encode_service_response(self, service: str, payload: dict) -> dict:
        data = dict(payload or {})
        if service == "frame" and isinstance(data.get("png"), bytes):
            data["png"] = base64.b64encode(data["png"]).decode("ascii")
        return data

    def decode_service_response(self, service: str, payload: dict) -> dict:
        data = dict(payload or {})
        if service == "frame" and isinstance(data.get("png"), str) and data["png"]:
            data["png"] = base64.b64decode(data["png"])
        return data


class BusCompatibilityBridge:
    def __init__(
        self,
        local_bus: LocalRuntimeBus,
        remote_bus: RuntimeBusProtocol,
        *,
        mirror_topic_prefixes: list[str] | None = None,
        mirror_queue_size: int = 1024,
        codec: WireCodec | None = None,
    ) -> None:
        self._local = local_bus
        self._remote = remote_bus
        self._prefixes = list(mirror_topic_prefixes or [])
        self._codec = codec or WireCodec()
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(
            maxsize=max(1, mirror_queue_size)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dropped_count = 0
        self._lock = threading.Lock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def start(self) -> None:
        if self._thread is not None:
            return
        for service in self._local.service_names():
            self._remote.respond(
                service,
                lambda payload, service=service: self._codec.encode_service_response(
                    service,
                    self._local.request(
                        service,
                        self._codec.decode_service_request(service, payload),
                    ),
                ),
            )
        for prefix in self._prefixes:
            self._local.subscribe(prefix, self._enqueue_event)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="yuki-bus-compatibility-bridge",
        )
        self._thread.start()

    def health(self) -> dict:
        return {
            "healthy": self._thread is not None and self._thread.is_alive(),
            "queue_depth": self._queue.qsize(),
            "dropped_count": self.dropped_count,
        }

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _enqueue_event(self, topic: str, payload: Any) -> None:
        try:
            self._queue.put_nowait((topic, payload))
        except queue.Full:
            with self._lock:
                self._dropped_count += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            topic, payload = item
            try:
                self._remote.publish(topic, self._codec.encode_event(topic, payload))
            except Exception:
                with self._lock:
                    self._dropped_count += 1
