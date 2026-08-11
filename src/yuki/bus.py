import json
import logging
import threading
import time
import uuid
from typing import Callable

import zmq

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id

logger = get_logger("yuki.bus")

VERSION = 1


class BusError(Exception):
    pass


class BusTimeoutError(BusError):
    pass


def _matches(prefix: str, topic: str) -> bool:
    return topic.startswith(prefix)


class MessageBus:
    """本地消息总线：PUB/SUB 经 XPUB/XSUB 枢纽 + REQ/REP 经 ROUTER/DEALER 枢纽。

    role="hub"：承载枢纽（绑定 base_port..base_port+2）。
    role="node"：仅连接。多进程部署时 bus_server 以 hub 运行，其余以 node 连接。
    """

    def __init__(
        self,
        base_port: int = 5555,
        role: str = "hub",
        hwm: int = 1000,
        register_interval: float = 10.0,
    ):
        self._ctx = zmq.Context()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._hwm = hwm
        self._register_interval = register_interval
        self._handlers: dict[str, list[Callable[[str, dict], None]]] = {}
        self._services: dict[str, Callable[[dict], dict]] = {}
        self._pending: dict[str, dict] = {}
        self._service_map: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._error_count = 0
        if role == "hub":
            self._start_hub()
        elif role != "node":
            raise ValueError(f"unknown bus role: {role!r}")

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, self._hwm)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.SNDHWM, self._hwm)
        self._dealer.setsockopt(zmq.RCVHWM, self._hwm)
        self._dealer.connect(f"tcp://127.0.0.1:{self._router_port}")
        threading.Thread(target=self._dealer_loop, daemon=True).start()
        threading.Thread(target=self._register_loop, daemon=True).start()

    def _start_hub(self) -> None:
        xsub = self._ctx.socket(zmq.XSUB)
        xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        xpub = self._ctx.socket(zmq.XPUB)
        xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.RCVHWM, self._hwm)
        self._router.bind(f"tcp://127.0.0.1:{self._router_port}")
        threading.Thread(target=self._proxy_loop, args=(xsub, xpub), daemon=True).start()
        threading.Thread(target=self._router_loop, daemon=True).start()

    def _proxy_loop(self, xsub, xpub) -> None:
        try:
            zmq.proxy(xsub, xpub)
        except zmq.ZMQError:
            pass

    def _router_loop(self) -> None:
        while True:
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
                msg = json.loads(f2.decode())
            except ValueError:
                continue
            if "payload" in msg:
                provider = self._service_map.get(msg.get("service", ""))
                if provider is None:
                    err = {"version": VERSION, "request_id": msg.get("request_id"), "error": "service not found"}
                    self._router.send_multipart([sender, json.dumps(err).encode()])
                else:
                    self._router.send_multipart([provider, sender, f2])
            elif "error" in msg or "result" in msg:
                self._router.send_multipart([f1, f2])

    def _dealer_loop(self) -> None:
        while True:
            try:
                frames = self._dealer.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    msg = json.loads(raw.decode())
                except ValueError:
                    continue
                if msg.get("trace_id"):
                    bind_trace_id(msg["trace_id"])
                handler = self._services.get(msg.get("service"))
                if handler is None:
                    reply = {"version": VERSION, "request_id": msg.get("request_id"), "error": "service not found"}
                else:
                    try:
                        result = handler(msg.get("payload", {}))
                        reply = {"version": VERSION, "request_id": msg.get("request_id"), "result": result}
                    except Exception:
                        logger.error("responder handler failed", service=msg.get("service"))
                        self._error_count += 1
                        reply = {"version": VERSION, "request_id": msg.get("request_id"), "error": "handler error"}
                self._dealer.send_multipart([client_id, json.dumps(reply).encode()])
                if msg.get("trace_id"):
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    msg = json.loads(frames[0].decode())
                except ValueError:
                    continue
                rid = msg.get("request_id")
                with self._lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = msg
                    entry["event"].set()

    def _register_loop(self) -> None:
        while True:
            try:
                time.sleep(self._register_interval)
            except Exception:
                return
            services = list(self._services.keys())
            if not services:
                continue
            try:
                for service in services:
                    self._dealer.send_multipart([b"REGISTER", service.encode()])
            except zmq.ZMQError:
                return

    def publish(self, topic: str, payload: dict) -> None:
        envelope = {"version": VERSION, "topic": topic, "payload": payload}
        self._pub.send_multipart([topic.encode(), json.dumps(envelope).encode()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            handlers = self._handlers.setdefault(topic_prefix, [])
            handlers.append(handler)
        if not hasattr(self, "_sub"):
            self._sub = self._ctx.socket(zmq.SUB)
            self._sub.setsockopt(zmq.RCVHWM, self._hwm)
            self._sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")
            threading.Thread(target=self._run_sub, args=(self._sub,), daemon=True).start()
        self._sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)

    def _run_sub(self, sub) -> None:
        while True:
            try:
                raw_topic, raw_payload = sub.recv_multipart()
            except zmq.ZMQError:
                return
            topic = raw_topic.decode()
            try:
                envelope = json.loads(raw_payload.decode())
            except ValueError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            payload = envelope.get("payload", envelope)
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

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        envelope = {
            "version": VERSION,
            "trace_id": uuid.uuid4().hex,
            "service": service,
            "request_id": rid,
            "payload": payload,
        }
        with self._lock:
            self._pending[rid] = {"event": event, "result": None}
        self._dealer.send_multipart([service.encode(), json.dumps(envelope).encode()])
        if not event.wait(timeout_ms / 1000.0):
            with self._lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._lock:
            msg = self._pending.pop(rid)["result"]
        if "error" in msg:
            raise BusError(msg["error"])
        return msg["result"]

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        self._services[service] = handler
        self._dealer.send_multipart([b"REGISTER", service.encode()])

    def close(self) -> None:
        try:
            self._ctx.destroy(linger=0)
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()
