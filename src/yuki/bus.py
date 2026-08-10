import json
import threading
from typing import Callable

import zmq


class MessageBus:
    """本地消息总线：PUB/SUB 事件 + REQ/REP 服务调用，仅 localhost。"""

    def __init__(self, base_port: int = 5555):
        self._ctx = zmq.Context()
        self._pub_port = base_port
        self._rep_port = base_port + 1
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.bind(f"tcp://127.0.0.1:{self._pub_port}")
        self._handlers: dict[str, Callable[[str, dict], None]] = {}
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict) -> None:
        self._pub.send_multipart([topic.encode(), json.dumps(payload).encode()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._handlers[topic_prefix] = handler
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(f"tcp://127.0.0.1:{self._pub_port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)
        thread = threading.Thread(target=self._run_sub, args=(sub,), daemon=True)
        thread.start()

    def _run_sub(self, sub) -> None:
        while True:
            raw_topic, raw_payload = sub.recv_multipart()
            topic = raw_topic.decode()
            payload = json.loads(raw_payload.decode())
            with self._lock:
                handler = self._find_handler(topic)
            if handler is not None:
                handler(topic, payload)

    def _find_handler(self, topic: str) -> Callable[[str, dict], None] | None:
        for prefix, handler in self._handlers.items():
            if topic.startswith(prefix):
                return handler
        return None

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
                msg = rep.recv_json()
                if msg["service"] == service:
                    result = handler(msg["payload"])
                    rep.send_json({"ok": True, "result": result})

        threading.Thread(target=loop, daemon=True).start()
