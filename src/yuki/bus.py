import logging
import os
import queue
import hmac
import time
import threading
import uuid
from typing import Callable

import zmq
from google.protobuf.message import DecodeError

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id
from yuki.proto.codec import (
    MAX_SUPPORTED_VERSION,
    VERSION,
    build_event,
    build_request,
    build_response_error,
    build_response_result,
    event_payload,
    parse_envelope,
    request_payload,
    response_result,
    version_supported,
)

logger = get_logger("yuki.bus")

# Hub 侧服务路由租约：provider 必须周期性 REGISTER 续租，过期后转为
# service not found，而不是把请求路由到已经死掉的 DEALER identity。
SERVICE_TTL_S = 30.0

# BusHub 内置 liveness 服务。bus_server 进程不挂普通 health responder，
# Supervisor 通过它区分“进程活着但总线已经死掉”的假活状态。
BUS_HEALTH_SERVICE = "health/bus_server"

# proxy 线程超过该时长未更新心跳即视为总线发布面不健康。
PROXY_STALE_S = 5.0

def _token_ok(expected: str, actual: bytes | str) -> bool:
    if not expected:
        return True
    if isinstance(actual, str):
        actual = actual.encode("utf-8")
    return hmac.compare_digest(expected.encode("utf-8"), actual)


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

    def _spawn(self, target, name: str | None = None) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True, name=name)
        thread.start()
        self._threads.append(thread)
        return thread

    def _on_stop(self) -> None:
        """子类在 join 线程前唤醒阻塞中的 worker（如放 sentinel）。"""
        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._on_stop()
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

    def __init__(self, base_port: int = 5555, hwm: int = 1000,
                 auth_token: str = "", max_msg_size: int = 10 * 1024 * 1024) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._service_map: dict[str, tuple[bytes, float]] = {}
        self._auth_token = auth_token
        self._max_msg_size = max_msg_size

        self._last_proxy_activity = time.monotonic()
        self._last_proxy_forwarded = time.monotonic()
        self._started_at = time.time()
        self._xsub = self._ctx.socket(zmq.XSUB)
        self._xsub.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        self._xpub = self._ctx.socket(zmq.XPUB)
        self._xpub.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.RCVHWM, hwm)
        self._router.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._router.bind(f"tcp://127.0.0.1:{self._router_port}")
        self._spawn(self._proxy_loop)
        self._spawn(self._router_loop)

    def _proxy_loop(self) -> None:
        while not self._stop.is_set():

            self._last_proxy_activity = time.monotonic()
            poller = zmq.Poller()
            poller.register(self._xsub, zmq.POLLIN)
            poller.register(self._xpub, zmq.POLLIN)
            events = dict(poller.poll(100))
            try:
                forwarded = False
                if self._xsub in events:
                    frames = self._xsub.recv_multipart()
                    if self._auth_token and (
                        len(frames) != 3 or not _token_ok(self._auth_token, frames[1])
                    ):
                        logger.warning("dropping unauthorized publish")
                        continue
                    self._xpub.send_multipart(frames)
                    forwarded = True
                if self._xpub in events:
                    frames = self._xpub.recv_multipart()
                    self._xsub.send_multipart(frames)
                    forwarded = True
                if forwarded:
                    self._last_proxy_forwarded = time.monotonic()
            except zmq.ZMQError:
                return
        self._close_socket(self._xsub)
        self._close_socket(self._xpub)


    def _purge_stale_services(self, now: float) -> None:
        stale = [
            service
            for service, (_, registered_at) in self._service_map.items()
            if now - registered_at > SERVICE_TTL_S
        ]
        for service in stale:
            self._service_map.pop(service, None)
            logger.warning("service route expired", service=service)

    def _collect_health(self) -> dict:
        now = time.monotonic()
        proxy_heartbeat_age = now - self._last_proxy_activity
        proxy_alive = proxy_heartbeat_age < PROXY_STALE_S
        forward_age = now - self._last_proxy_forwarded
        return {
            "process": "bus_server",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 2),
            "error_count": 0,
            "healthy": proxy_alive,
            "components": {
                "proxy": {
                    "ok": proxy_alive,
                    "last_forwarded_s": round(forward_age, 3),
                    "last_heartbeat_s": round(proxy_heartbeat_age, 3),
                },
                "router": {"ok": True},
            },
        }

    def health_snapshot(self) -> dict:
        """Return a thread-safe snapshot for an in-process BusHub owner."""
        return self._collect_health()

    def _router_loop(self) -> None:
        while not self._stop.is_set():
            self._purge_stale_services(time.monotonic())
            if not self._router.poll(100):
                continue
            try:
                frames = self._router.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) < 3:
                continue
            sender = frames[0]

            if b"REGISTER" == frames[1]:  # auth-aware registration
                version = 1
                if self._auth_token:
                    if len(frames) not in (4, 5) or not _token_ok(self._auth_token, frames[2]):
                        logger.warning("dropping unauthorized REGISTER")
                        continue
                    service_frame = frames[3]
                    version_frame = frames[4] if len(frames) == 5 else None
                else:
                    if len(frames) not in (3, 4):
                        logger.warning("dropping malformed REGISTER frame count")
                        continue
                    service_frame = frames[2]
                    version_frame = frames[3] if len(frames) == 4 else None
                if version_frame is not None:
                    try:
                        version = int(version_frame.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        logger.warning("dropping REGISTER with malformed version frame")
                        continue
                if version > MAX_SUPPORTED_VERSION:
                    logger.warning(
                        "rejecting REGISTER from incompatible version",
                        version=version,
                    )
                    continue
                try:
                    service = service_frame.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("dropping malformed REGISTER frame")
                    continue
                if not service:
                    logger.warning("dropping empty REGISTER service name")
                    continue
                self._service_map[service] = (sender, time.monotonic())
                continue
            raw = frames[-1] if self._auth_token else frames[2]
            f1 = frames[1]
            try:
                envelope = parse_envelope(raw)
            except DecodeError:
                logger.warning("dropping malformed envelope from %s", sender)
                continue
            if not version_supported(envelope):
                logger.warning("dropping unsupported envelope version", version=envelope.version)
                continue
            kind = envelope.WhichOneof("body")
            if kind == "request":
                if self._auth_token and (
                    len(frames) != 4 or not _token_ok(self._auth_token, frames[2])
                ):
                    logger.warning("rejecting unauthorized request")
                    err = build_response_error(
                        envelope.request.request_id, "unauthorized",
                        trace_id=envelope.trace_id,
                    )
                    self._router.send_multipart([sender, err.SerializeToString()])
                    continue
                service = envelope.request.service
                if service == BUS_HEALTH_SERVICE:
                    reply = build_response_result(
                        envelope.request.request_id, self._collect_health(),
                        trace_id=envelope.trace_id,
                    )
                    self._router.send_multipart([sender, reply.SerializeToString()])
                    continue
                entry = self._service_map.get(service)
                provider = entry[0] if entry else None
                if not provider:
                    err = build_response_error(
                        envelope.request.request_id, "service not found",
                        trace_id=envelope.trace_id,
                    )
                    self._router.send_multipart([sender, err.SerializeToString()])
                else:
                    self._router.send_multipart([provider, sender, raw])
            elif kind == "response":
                self._router.send_multipart([f1, raw])
            else:
                logger.warning("unknown oneof kind in router", kind=kind)
        self._close_socket(self._router)


class BusNode(_Base):
    """节点：PUB/DEALER/SUB，publish/subscribe/request/respond。"""

    supports_response_lanes = True

    def __init__(
        self,
        base_port: int = 5555,
        hwm: int = 1000,
        register_interval: float = 10.0,
        subscriber_queue_size: int = 256,
        auth_token: str = "",
        max_msg_size: int = 10 * 1024 * 1024,
        responder_queue_size: int | None = None,
        control_workers: int = 1,
        work_workers: int = 4,
        stream_workers: int = 2,
    ) -> None:
        super().__init__()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._hwm = hwm
        self._subscriber_queue_size = max(1, subscriber_queue_size)
        self._auth_token = auth_token
        self._max_msg_size = max_msg_size
        self._register_interval = register_interval
        self._handlers: dict[str, list[Callable[[str, dict], None]]] = {}
        self._services: dict[str, tuple[Callable[[dict], dict], str]] = {}
        self._pending: dict[str, dict] = {}
        self._handlers_lock = threading.Lock()
        # 兼容旧代码路径：订阅/请求/响应共用一把互斥锁即可满足
        # 多线程安全；新代码优先使用语义更明确的分区锁。
        self._lock = self._handlers_lock
        self._handler_queues: dict[int, queue.Queue] = {}
        self._services_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._error_count = 0
        self._dropped_count = 0
        self._counts_lock = threading.Lock()

        self._pub_queue: queue.Queue = queue.Queue(maxsize=max(1, hwm))
        self._dealer_outbox: queue.Queue = queue.Queue(maxsize=max(1, hwm * 2))
        lane_queue_size = max(1, responder_queue_size or hwm)
        self._responder_queues: dict[str, queue.Queue] = {
            "control": queue.Queue(maxsize=lane_queue_size),
            "work": queue.Queue(maxsize=lane_queue_size),
            "stream": queue.Queue(maxsize=lane_queue_size),
        }
        self._sub_cmds: queue.Queue = queue.Queue()
        self._loop_heartbeats = {
            "pub": time.monotonic(),
            "dealer": time.monotonic(),
            "sub": time.monotonic(),
        }
        self._subscriptions_enabled = threading.Event()
        self._subscriptions_enabled.set()
        self._pending_subscriptions: list[str] = []
        self._error_count = 0

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, hwm)
        self._pub.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.SNDHWM, hwm)
        self._dealer.setsockopt(zmq.RCVHWM, hwm)
        self._dealer.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._dealer.connect(f"tcp://127.0.0.1:{self._router_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.setsockopt(zmq.MAXMSGSIZE, self._max_msg_size)
        self._sub.setsockopt(zmq.RCVHWM, hwm)
        self._sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")

        self._spawn(self._pub_loop, name="yuki-bus-pub")
        self._spawn(self._dealer_loop, name="yuki-bus-dealer")
        self._spawn(self._register_loop, name="yuki-bus-register")
        self._spawn(self._run_sub, name="yuki-bus-sub")
        for lane, worker_count in {
            "control": control_workers,
            "work": work_workers,
            "stream": stream_workers,
        }.items():
            for index in range(max(1, int(worker_count))):
                self._spawn(
                    lambda lane=lane: self._responder_worker(lane),
                    name=f"yuki-bus-responder:{lane}:{index}",
                )

    @property
    def error_count(self) -> int:
        with self._counts_lock:
            return self._error_count

    @property
    def dropped_count(self) -> int:
        with self._counts_lock:
            return self._dropped_count


    def bus_health(self) -> dict:
        now = time.monotonic()
        with self._counts_lock:
            ages = {
                name: round(now - heartbeat, 3)
                for name, heartbeat in self._loop_heartbeats.items()
            }
            dropped = self._dropped_count
        return {
            "healthy": all(age < PROXY_STALE_S for age in ages.values()),
            "threads": ages,
            "dropped_count": dropped,
        }

    def _bump_error(self, n: int = 1) -> None:
        with self._counts_lock:
            self._error_count += n

    def _bump_dropped(self, n: int = 1) -> None:
        with self._counts_lock:
            self._dropped_count += n

    def _enqueue_dealer(self, frames: list, timeout_s: float | None = None) -> bool:
        try:
            if timeout_s is None:
                self._dealer_outbox.put_nowait(frames)
            else:
                self._dealer_outbox.put(frames, timeout=timeout_s)
            return True
        except queue.Full:
            return False

    def _register_frames(self, service: str) -> list:
        frames = [b"REGISTER"]
        if self._auth_token:
            frames.append(self._auth_token.encode("utf-8"))
        frames.append(service.encode())
        frames.append(str(VERSION).encode("utf-8"))
        return frames

    def publish(self, topic: str, payload: dict, *, trace_id: str | None = None) -> None:
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        envelope = build_event(topic, payload, trace_id=trace_id)
        frames = [topic.encode()]
        if self._auth_token:
            frames.append(self._auth_token.encode("utf-8"))
        frames.append(envelope.SerializeToString())
        try:
            self._pub_queue.put_nowait(frames)
        except queue.Full:
            self._bump_dropped()
            logger.warning("publish queue full, dropping event", topic=topic)

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            handlers = self._handlers.setdefault(topic_prefix, [])
            handlers.append(handler)

            worker_queue: queue.Queue = queue.Queue(maxsize=self._subscriber_queue_size)
            self._handler_queues[id(handler)] = worker_queue
            thread = threading.Thread(
                target=self._handler_worker,
                args=(handler, worker_queue),
                daemon=True,
                name=f"yuki-bus-handler:{topic_prefix}",
            )
            thread.start()
            self._threads.append(thread)
        self._sub_cmds.put(("subscribe", topic_prefix))

    def pause_subscriptions(self) -> None:
        """暂缓应用 SUBSCRIBE，用于 setup 期间装配多个订阅而不漏早期事件。"""
        self._subscriptions_enabled.clear()

    def resume_subscriptions(self) -> None:
        self._subscriptions_enabled.set()
        self._sub_cmds.put(("resume", None))

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        trace_id = uuid.uuid4().hex
        envelope = build_request(service, rid, trace_id, payload)
        with self._pending_lock:
            self._pending[rid] = {"event": event, "result": None}
        frames = [service.encode()]
        if self._auth_token:
            frames.append(self._auth_token.encode("utf-8"))
        frames.append(envelope.SerializeToString())
        if not self._enqueue_dealer(
            frames, timeout_s=min(max(timeout_ms / 1000.0, 0.1), 2.0)
        ):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(
                f"request to {service!r} dropped: dealer outbox full"
            )
        if not event.wait(timeout_ms / 1000.0):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._pending_lock:
            resp = self._pending.pop(rid)["result"]
        if resp is None:
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        if resp.response.HasField("error"):
            raise BusError(resp.response.error)
        return response_result(resp)

    def respond(
        self,
        service: str,
        handler: Callable[[dict], dict],
        *,
        lane: str = "work",
    ) -> None:
        if lane not in self._responder_queues:
            raise ValueError(f"unknown responder lane: {lane}")
        with self._services_lock:
            self._services[service] = (handler, lane)
        if not self._enqueue_dealer(self._register_frames(service)):
            logger.warning(
                "REGISTER frame dropped (dealer outbox full)", service=service
            )


    def _on_stop(self) -> None:
        with self._handlers_lock:
            for worker_queue in self._handler_queues.values():
                try:
                    worker_queue.put_nowait(None)
                except queue.Full:
                    # 队列已满时 worker 正在退出或积压；关闭阶段由 daemon 线程兜底。
                    pass
        for responder_queue in self._responder_queues.values():
            for _ in range(8):
                try:
                    responder_queue.put_nowait(None)
                except queue.Full:
                    try:
                        responder_queue.get_nowait()
                    except queue.Empty:
                        break

    def _responder_worker(self, lane: str) -> None:
        responder_queue = self._responder_queues[lane]
        while not self._stop.is_set():
            try:
                item = responder_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                return
            client_id, envelope, service, handler = item
            if envelope.trace_id:
                bind_trace_id(envelope.trace_id)
            try:
                try:
                    result = handler(request_payload(envelope))
                    reply = build_response_result(
                        envelope.request.request_id,
                        result,
                        trace_id=envelope.trace_id,
                    )
                except Exception:
                    logger.error("responder handler failed", service=service, exc_info=True)
                    self._bump_error()
                    reply = build_response_error(
                        envelope.request.request_id,
                        "handler error",
                        trace_id=envelope.trace_id,
                    )
                if not self._enqueue_dealer([client_id, reply.SerializeToString()]):
                    self._bump_dropped()
                    logger.warning("responder outbox full, dropping response", service=service)
            finally:
                if envelope.trace_id:
                    unbind_trace_id()

    def _handler_worker(self, handler, worker_queue: queue.Queue) -> None:
        while True:
            item = worker_queue.get()
            if item is None:
                return
            topic, payload, trace_id = item
            if trace_id:
                bind_trace_id(trace_id)
            try:
                handler(topic, payload)
            except Exception:
                logger.error("subscriber handler failed", topic=topic, exc_info=True)
                self._bump_error()
            finally:
                if trace_id:
                    unbind_trace_id()

    def _pub_loop(self) -> None:
        self._loop_heartbeats["pub"] = time.monotonic()
        while not self._stop.is_set():
            self._loop_heartbeats["pub"] = time.monotonic()
            try:
                frames = self._pub_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._pub.send_multipart(frames)
            except zmq.ZMQError:
                logger.error("pub loop stopped", exc_info=True)
                self._bump_error()
                return
    def _dealer_loop(self) -> None:
        while not self._stop.is_set():
            self._loop_heartbeats["dealer"] = time.monotonic()
            while True:
                try:
                    frames = self._dealer_outbox.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dealer.send_multipart(frames)
                except zmq.ZMQError:
                    logger.error("dealer loop stopped", exc_info=True)
                    self._bump_error()
                    return
            if not self._dealer.poll(50):
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
                if not version_supported(envelope):
                    logger.warning("dropping unsupported request version", version=envelope.version)
                    continue
                kind = envelope.WhichOneof("body")
                if kind != "request":
                    logger.warning("unknown oneof kind in dealer request path", kind=kind)
                    continue
                service = envelope.request.service
                with self._services_lock:
                    registration = self._services.get(service)
                if registration is None:
                    reply = build_response_error(
                        envelope.request.request_id,
                        "service not found",
                        trace_id=envelope.trace_id,
                    )
                    self._enqueue_dealer([client_id, reply.SerializeToString()])
                    continue
                handler, lane = registration
                try:
                    self._responder_queues[lane].put_nowait(
                        (client_id, envelope, service, handler)
                    )
                except queue.Full:
                    self._bump_dropped()
                    reply = build_response_error(
                        envelope.request.request_id,
                        "server busy",
                        trace_id=envelope.trace_id,
                    )
                    self._enqueue_dealer([client_id, reply.SerializeToString()])
            elif len(frames) == 1:
                try:
                    envelope = parse_envelope(frames[0])
                except DecodeError:
                    continue
                if not version_supported(envelope):
                    logger.warning(
                        "dropping unsupported response version",
                        version=envelope.version,
                    )
                    continue
                kind = envelope.WhichOneof("body")
                if kind != "response":
                    logger.warning("unknown oneof kind in dealer response path", kind=kind)
                    continue
                rid = envelope.response.request_id
                with self._pending_lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = envelope
                    entry["event"].set()
        self._close_socket(self._dealer)

    def _register_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=self._register_interval)
            if self._stop.is_set():
                return
            with self._services_lock:
                services = list(self._services.keys())
            if not services:
                continue
            try:
                for service in services:
                    if not self._enqueue_dealer(self._register_frames(service)):
                        logger.warning(
                            "REGISTER refresh dropped (dealer outbox full)",
                            service=service,
                        )
            except zmq.ZMQError:
                return

    def _run_sub(self) -> None:
        while not self._stop.is_set():
            self._loop_heartbeats["sub"] = time.monotonic()
            while True:
                try:
                    command = self._sub_cmds.get_nowait()
                except queue.Empty:
                    break
                if command[0] == "resume":
                    for pending_prefix in self._pending_subscriptions:
                        self._sub.setsockopt_string(zmq.SUBSCRIBE, pending_prefix)
                    self._pending_subscriptions.clear()
                    continue
                kind, prefix = command
                if kind == "subscribe":
                    if self._subscriptions_enabled.is_set():
                        self._sub.setsockopt_string(zmq.SUBSCRIBE, str(prefix))
                    else:
                        self._pending_subscriptions.append(prefix)
            if not self._sub.poll(20):
                continue
            try:
                frames = self._sub.recv_multipart()
                raw_topic = frames[0]
                if self._auth_token:
                    if len(frames) != 3 or not _token_ok(self._auth_token, frames[1]):
                        logger.warning("dropping unauthorized event")
                        continue
                    raw_payload = frames[2]
                else:
                    if len(frames) not in (2, 3):
                        logger.warning("dropping malformed event frame count")
                        continue
                    raw_payload = frames[-1]
            except zmq.ZMQError:
                return
            try:
                topic = raw_topic.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("dropping message with malformed topic")
                continue
            try:
                envelope = parse_envelope(raw_payload)
            except DecodeError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            if not version_supported(envelope):
                logger.warning(
                    "dropping unsupported event version",
                    topic=topic,
                    version=envelope.version,
                )
                continue
            kind = envelope.WhichOneof("body")
            if kind != "event":
                logger.warning("unknown oneof kind in sub path", topic=topic, kind=kind)
                continue
            payload = event_payload(envelope)
            trace_id = envelope.trace_id or ""
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                worker_queue = self._handler_queues.get(id(handler))
                try:
                    if worker_queue is None:
                        continue
                    try:
                        worker_queue.put_nowait((topic, payload, trace_id))
                    except queue.Full:
                        self._bump_dropped()
                        logger.warning(
                            "subscriber queue full, dropping event", topic=topic
                        )
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._bump_error()
        self._close_socket(getattr(self, "_sub", None))
