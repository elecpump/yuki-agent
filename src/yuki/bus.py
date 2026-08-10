import json
import os
import threading
from typing import Callable

import zmq

_WINDOWS_ROOT = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
os.environ.setdefault("SystemRoot", _WINDOWS_ROOT)
os.environ.setdefault("windir", _WINDOWS_ROOT)


def _find_handler(topic: str, handlers: dict[str, Callable[[str, dict], None]]) -> Callable[[str, dict], None] | None:
    for prefix, handler in handlers.items():
        if topic.startswith(prefix):
            return handler
    return None


def _run_sub(sub, handlers: dict[str, Callable[[str, dict], None]], lock: threading.Lock) -> None:
    while True:
        try:
            raw_topic, raw_payload = sub.recv_multipart()
        except zmq.ZMQError:
            return
        topic = raw_topic.decode()
        payload = json.loads(raw_payload.decode())
        with lock:
            handler = _find_handler(topic, handlers)
        if handler is not None:
            handler(topic, payload)


class MessageBus:
    """本地消息总线：PUB/SUB 经 XPUB/XSUB 枢纽转发 + REQ/REP 服务调用，仅 localhost。

    role="hub"：承载转发枢纽（绑定端口），本进程同时作为普通节点收发。
    role="node"：仅连接枢纽，发布与订阅都经枢纽转发。多进程部署时
    只允许一个进程以 hub 角色运行，其余进程以 node 角色连接。
    """

    def __init__(self, base_port: int = 5555, role: str = "hub"):
        self._ctx = zmq.Context()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._rep_port = base_port + 2
        self._handlers: dict[str, Callable[[str, dict], None]] = {}
        self._lock = threading.Lock()
        if role == "hub":
            self._start_hub()
        elif role != "node":
            raise ValueError(f"unknown bus role: {role!r}")
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

    def _start_hub(self) -> None:
        xsub = self._ctx.socket(zmq.XSUB)
        xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        xpub = self._ctx.socket(zmq.XPUB)
        xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")

        def proxy() -> None:
            try:
                zmq.proxy(xsub, xpub)
            except zmq.ZMQError:
                pass

        threading.Thread(target=proxy, daemon=True).start()

    def publish(self, topic: str, payload: dict) -> None:
        self._pub.send_multipart([topic.encode(), json.dumps(payload).encode()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._handlers[topic_prefix] = handler
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)
        thread = threading.Thread(
            target=_run_sub, args=(sub, self._handlers, self._lock), daemon=True
        )
        thread.start()

    def request(self, service: str, payload: dict) -> dict:
        req = self._ctx.socket(zmq.REQ)
        req.connect(f"tcp://127.0.0.1:{self._rep_port}")
        req.send_json({"service": service, "payload": payload})
        result = req.recv_json()
        req.close()
        return result["result"]

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        rep = self._ctx.socket(zmq.REP)
        rep.bind(f"tcp://127.0.0.1:{self._rep_port}")

        def loop() -> None:
            while True:
                try:
                    msg = rep.recv_json()
                except zmq.ZMQError:
                    return
                if msg["service"] == service:
                    result = handler(msg["payload"])
                    rep.send_json({"ok": True, "result": result})

        threading.Thread(target=loop, daemon=True).start()

    def close(self) -> None:
        try:
            self._ctx.destroy(linger=0)
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()
