from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from yuki.bus import BusError, BusTimeoutError


EventHandler = Callable[[str, Any], None]
ServiceHandler = Callable[[dict], dict]


class LocalServiceError(BusError):
    """A local service failed while preserving the public bus exception hierarchy."""


class LocalServiceCycleError(LocalServiceError):
    pass


@runtime_checkable
class RuntimeBusProtocol(Protocol):
    @property
    def error_count(self) -> int: ...

    @property
    def dropped_count(self) -> int: ...

    def publish(self, topic: str, payload: Any, *, trace_id: str | None = None) -> None: ...

    def subscribe(self, topic_prefix: str, handler: EventHandler) -> None: ...

    def respond(
        self,
        service: str,
        handler: ServiceHandler,
        *,
        lane: str = "work",
    ) -> None: ...

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict: ...

    def pause_subscriptions(self) -> None: ...

    def resume_subscriptions(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class _Subscriber:
    prefix: str
    handler: EventHandler
    work_queue: queue.Queue
    thread: threading.Thread


class LocalEventBus:
    """In-process prefix event dispatch with isolation per subscriber callback."""

    _STOP = object()

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        self._queue_size = max(1, int(subscriber_queue_size))
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._counts_lock = threading.Lock()
        self._subscriptions_enabled = threading.Event()
        self._subscriptions_enabled.set()
        self._closed = False
        self._error_count = 0
        self._dropped_count = 0

    @property
    def error_count(self) -> int:
        with self._counts_lock:
            return self._error_count

    @property
    def dropped_count(self) -> int:
        with self._counts_lock:
            return self._dropped_count

    def subscribe(self, topic_prefix: str, handler: EventHandler) -> None:
        if not callable(handler):
            raise TypeError("event handler must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("local event bus is closed")
            work_queue: queue.Queue = queue.Queue(maxsize=self._queue_size)
            thread = threading.Thread(
                target=self._worker,
                args=(handler, work_queue),
                daemon=True,
                name=f"yuki-local-event:{topic_prefix}",
            )
            subscriber = _Subscriber(topic_prefix, handler, work_queue, thread)
            self._subscribers.append(subscriber)
            thread.start()

    def publish(self, topic: str, payload: Any) -> None:
        if self._closed or not self._subscriptions_enabled.is_set():
            return
        with self._lock:
            subscribers = [item for item in self._subscribers if topic.startswith(item.prefix)]
        for subscriber in subscribers:
            try:
                subscriber.work_queue.put_nowait((topic, payload))
            except queue.Full:
                with self._counts_lock:
                    self._dropped_count += 1

    def pause_subscriptions(self) -> None:
        self._subscriptions_enabled.clear()

    def resume_subscriptions(self) -> None:
        self._subscriptions_enabled.set()

    def health(self) -> dict:
        with self._lock:
            workers = [item.thread.is_alive() for item in self._subscribers]
        return {
            "healthy": not self._closed and all(workers),
            "subscribers": len(workers),
            "error_count": self.error_count,
            "dropped_count": self.dropped_count,
            "paused": not self._subscriptions_enabled.is_set(),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.work_queue.put_nowait(self._STOP)
            except queue.Full:
                try:
                    subscriber.work_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.work_queue.put_nowait(self._STOP)
                except queue.Full:
                    pass
        for subscriber in subscribers:
            subscriber.thread.join(timeout=2.0)

    def _worker(self, handler: EventHandler, work_queue: queue.Queue) -> None:
        while True:
            item = work_queue.get()
            if item is self._STOP:
                return
            topic, payload = item
            try:
                handler(topic, payload)
            except Exception:
                with self._counts_lock:
                    self._error_count += 1


class LocalServiceRegistry:
    """Synchronous in-process services with cycle detection and deadline accounting."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceHandler] = {}
        self._lock = threading.Lock()
        self._counts_lock = threading.Lock()
        self._call_stack = threading.local()
        self._closed = False
        self._error_count = 0
        self._timeout_count = 0

    @property
    def error_count(self) -> int:
        with self._counts_lock:
            return self._error_count

    @property
    def timeout_count(self) -> int:
        with self._counts_lock:
            return self._timeout_count

    def respond(self, service: str, handler: ServiceHandler) -> None:
        if not service:
            raise ValueError("service name is required")
        if not callable(handler):
            raise TypeError("service handler must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("local service registry is closed")
            self._services[service] = handler

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        with self._lock:
            handler = self._services.get(service)
            closed = self._closed
        if closed:
            raise LocalServiceError("local service registry is closed")
        if handler is None:
            raise LocalServiceError("service not found")

        stack = list(getattr(self._call_stack, "services", ()))
        if service in stack:
            cycle = " -> ".join([*stack, service])
            self._bump_error()
            raise LocalServiceCycleError(f"local service cycle detected: {cycle}")

        stack.append(service)
        self._call_stack.services = stack
        started = time.monotonic()
        try:
            try:
                result = handler(dict(payload or {}))
            except (LocalServiceCycleError, BusTimeoutError):
                raise
            except Exception as exc:
                self._bump_error()
                raise LocalServiceError(f"handler error: {service}") from exc
        finally:
            stack.pop()
            self._call_stack.services = stack

        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms > max(0, timeout_ms):
            with self._counts_lock:
                self._timeout_count += 1
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        if not isinstance(result, dict):
            self._bump_error()
            raise LocalServiceError(f"handler returned non-dict result: {service}")
        return result

    def health(self) -> dict:
        with self._lock:
            service_count = len(self._services)
        return {
            "healthy": not self._closed,
            "services": service_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
        }

    def service_names(self) -> list[str]:
        with self._lock:
            return list(self._services)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._services.clear()

    def _bump_error(self) -> None:
        with self._counts_lock:
            self._error_count += 1


class LocalRuntimeBus:
    """Facade used by co-located agents after the process merge."""

    supports_response_lanes = True

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        self.events = LocalEventBus(subscriber_queue_size=subscriber_queue_size)
        self.services = LocalServiceRegistry()

    @property
    def error_count(self) -> int:
        return self.events.error_count + self.services.error_count

    @property
    def dropped_count(self) -> int:
        return self.events.dropped_count

    def publish(self, topic: str, payload: Any, *, trace_id: str | None = None) -> None:
        del trace_id
        self.events.publish(topic, payload)

    def subscribe(self, topic_prefix: str, handler: EventHandler) -> None:
        self.events.subscribe(topic_prefix, handler)

    def respond(
        self,
        service: str,
        handler: ServiceHandler,
        *,
        lane: str = "work",
    ) -> None:
        del lane
        self.services.respond(service, handler)

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        return self.services.request(service, payload, timeout_ms=timeout_ms)

    def pause_subscriptions(self) -> None:
        self.events.pause_subscriptions()

    def resume_subscriptions(self) -> None:
        self.events.resume_subscriptions()

    def bus_health(self) -> dict:
        return self.health()

    def service_names(self) -> list[str]:
        return self.services.service_names()

    def health(self) -> dict:
        event_health = self.events.health()
        service_health = self.services.health()
        return {
            "healthy": event_health["healthy"] and service_health["healthy"],
            "events": event_health,
            "services": service_health,
            "error_count": self.error_count,
            "dropped_count": self.dropped_count,
        }

    def close(self) -> None:
        self.services.close()
        self.events.close()
