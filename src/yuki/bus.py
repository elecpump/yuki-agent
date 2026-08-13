import logging
import threading
import uuid
from typing import Callable

import zmq
from google.protobuf.message import DecodeError

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id
from yuki.proto.codec import (
    build_event,
    build_request,
    build_response_error,
    build_response_result,
    event_payload,
    parse_envelope,
    request_payload,
    response_result,
)

logger = get_logger("yuki.bus")


class BusError(Exception):
    pass


class BusTimeoutError(BusError):
    pass


def _matches(prefix: str, topic: str) -> bool:
    return topic.startswith(prefix)


class _Base:
    """共享生命周期：线程跟踪、socket 关闭、libzmq 4.3.5 signaler 规避。"""

    def __init__(self) -> None:
        self._ctx = zmq.Context.instance()
        self._closed = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _spawn(self, target) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._close_socket(getattr(self, "_xsub", None))
        self._close_socket(getattr(self, "_xpub", None))
        self._close_socket(getattr(self, "_router", None))
        self._close_socket(getattr(self, "_pub", None))
        self._close_socket(getattr(self, "_dealer", None))
        self._close_socket(getattr(self, "_sub", None))

    def _close_socket(self, sock) -> None:
        if sock is not None:
            try:
                sock.close(linger=0)
            except zmq.ZMQError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class BusHub(_Base):
    """枢纽：XSUB/XPUB/ROUTER，只做转发与 REQ/REP 路由。"""

    def __init__(self, base_port: int = 5555, hwm: int = 1000) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._service_map: dict[str, bytes] = {}
        self._xsub = self._ctx.socket(zmq.XSUB)
        self._xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        self._xpub = self._ctx.socket(zmq.XPUB)
        self._xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.RCVHWM, hwm)
        self._router.bind(f"tcp://127.0.0.1:{self._router_port}")
        self._spawn(self._proxy_loop)
        self._spawn(self._router_loop)

    def _proxy_loop(self) -> None:
        while not self._stop.is_set():
            poller = zmq.Poller()
            poller.register(self._xsub, zmq.POLLIN)
            poller.register(self._xpub, zmq.POLLIN)
            events = dict(poller.poll(100))
            try:
                if self._xsub in events:
                    frames = self._xsub.recv_multipart()
                    self._xpub.send_multipart(frames)
                if self._xpub in events:
                    frames = self._xpub.recv_multipart()
                    self._xsub.send_multipart(frames)
            except zmq.ZMQError:
                return
        self._close_socket(self._xsub)
        self._close_socket(self._xpub)

    def _router_loop(self) -> None:
        while not self._stop.is_set():
            if not self._router.poll(100):
                continue
            try:
                frames = self._router.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) < 3:
                continue
            sender = frames[0]
            if frames[1] == b"REGISTER":
                self._service_map[frames[2].decode()] = sender
                continue
            f1, f2 = frames[1], frames[2]
            try:
                envelope = parse_envelope(f2)
            except DecodeError:
                logger.warning("dropping malformed envelope from %s", sender)
                continue
            kind = envelope.WhichOneof("body")
            if kind == "request":
                provider = self._service_map.get(envelope.request.service, "")
                if not provider:
                    err = build_response_error(envelope.request.request_id, "service not found")
                    self._router.send_multipart([sender, err.SerializeToString()])
                else:
                    self._router.send_multipart([provider, sender, f2])
            elif kind == "response":
                self._router.send_multipart([f1, f2])
        self._close_socket(self._router)


class BusNode(_Base):
    """节点：PUB/DEALER/SUB，publish/subscribe/request/respond。"""

    def __init__(
        self,
        base_port: int = 5555,
        hwm: int = 1000,
        register_interval: float = 10.0,
    ) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._hwm = hwm
        self._register_interval = register_interval
        self._handlers: dict[str, list[Callable[[str, dict], None]]] = {}
        self._services: dict[str, Callable[[dict], dict]] = {}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._error_count = 0

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, hwm)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.SNDHWM, hwm)
        self._dealer.setsockopt(zmq.RCVHWM, hwm)
        self._dealer.connect(f"tcp://127.0.0.1:{self._router_port}")
        self._spawn(self._dealer_loop)
        self._spawn(self._register_loop)

    @property
    def error_count(self) -> int:
        return self._error_count

    def publish(self, topic: str, payload: dict) -> None:
        envelope = build_event(topic, payload)
        self._pub.send_multipart([topic.encode(), envelope.SerializeToString()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            handlers = self._handlers.setdefault(topic_prefix, [])
            handlers.append(handler)
        if not hasattr(self, "_sub"):
            self._sub = self._ctx.socket(zmq.SUB)
            self._sub.setsockopt(zmq.RCVHWM, self._hwm)
            self._sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")
            self._spawn(self._run_sub)
        self._sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        trace_id = uuid.uuid4().hex
        envelope = build_request(service, rid, trace_id, payload)
        with self._lock:
            self._pending[rid] = {"event": event, "result": None}
        self._dealer.send_multipart([service.encode(), envelope.SerializeToString()])
        if not event.wait(timeout_ms / 1000.0):
            with self._lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._lock:
            resp = self._pending.pop(rid)["result"]
        if resp.response.HasField("error"):
            raise BusError(resp.response.error)
        return response_result(resp)

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        self._services[service] = handler
        self._dealer.send_multipart([b"REGISTER", service.encode()])

    def _dealer_loop(self) -> None:
        while not self._stop.is_set():
            if not self._dealer.poll(100):
                continue
            try:
                frames = self._dealer.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    envelope = parse_envelope(raw)
                except DecodeError:
                    continue
                if envelope.trace_id:
                    bind_trace_id(envelope.trace_id)
                if envelope.WhichOneof("body") != "request":
                    if envelope.trace_id:
                        unbind_trace_id()
                    continue
                handler = self._services.get(envelope.request.service)
                if handler is None:
                    reply = build_response_error(envelope.request.request_id, "service not found")
                else:
                    try:
                        result = handler(request_payload(envelope))
                        reply = build_response_result(envelope.request.request_id, result)
                    except Exception:
                        logger.error("responder handler failed", service=envelope.request.service)
                        self._error_count += 1
                        reply = build_response_error(envelope.request.request_id, "handler error")
                self._dealer.send_multipart([client_id, reply.SerializeToString()])
                if envelope.trace_id:
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    envelope = parse_envelope(frames[0])
                except DecodeError:
                    continue
                if envelope.WhichOneof("body") != "response":
                    continue
                rid = envelope.response.request_id
                with self._lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = envelope
                    entry["event"].set()
        self._close_socket(self._dealer)

    def _register_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=self._register_interval)
            services = list(self._services.keys())
            if not services:
                continue
            try:
                for service in services:
                    self._dealer.send_multipart([b"REGISTER", service.encode()])
            except zmq.ZMQError:
                return

    def _run_sub(self) -> None:
        while not self._stop.is_set():
            if not self._sub.poll(100):
                continue
            try:
                raw_topic, raw_payload = self._sub.recv_multipart()
            except zmq.ZMQError:
                return
            topic = raw_topic.decode()
            try:
                envelope = parse_envelope(raw_payload)
            except DecodeError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            if envelope.WhichOneof("body") != "event":
                logger.warning("dropping non-event envelope", topic=topic)
                continue
            payload = event_payload(envelope)
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                try:
                    handler(topic, payload)
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._error_count += 1
        self._close_socket(getattr(self, "_sub", None))
