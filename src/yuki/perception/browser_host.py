from __future__ import annotations

import json
import struct
import sys
import time

from yuki.bus import BusNode
from yuki.config import Config
from yuki.perception.text import TEXT_INGEST_SERVICE


def _read_message(stdin=None) -> dict | None:
    stdin = stdin or sys.stdin.buffer
    raw_length = stdin.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise EOFError("truncated native messaging length")
    message_length = struct.unpack("<I", raw_length)[0]
    if message_length <= 0:
        return {}
    raw = stdin.read(message_length)
    if len(raw) != message_length:
        raise EOFError("truncated native messaging payload")
    return json.loads(raw.decode("utf-8"))


def _write_message(payload: dict, stdout=None) -> None:
    stdout = stdout or sys.stdout.buffer
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stdout.write(struct.pack("<I", len(raw)))
    stdout.write(raw)
    stdout.flush()


def normalize_dom_message(message: dict) -> dict:
    return {
        "source": "dom",
        "text": str(message.get("text", "")),
        "title": str(message.get("title", "")),
        "url": str(message.get("url", "")),
        "hwnd": message.get("hwnd"),
        "frame_id": message.get("frame_id"),
        "confidence": float(message.get("confidence", 0.95)),
        "ts": float(message.get("ts") or time.time()),
        "reason": "native_dom",
    }


def run_host(bus=None, stdin=None, stdout=None) -> None:
    config = Config.from_env()
    bus = bus or BusNode(
        base_port=config.bus.base_port,
        hwm=config.bus.hwm,
        auth_token=config.bus.auth_token,
        max_msg_size=config.bus.max_msg_size,
    )
    try:
        while True:
            message = _read_message(stdin)
            if message is None:
                return
            result = bus.request(TEXT_INGEST_SERVICE, normalize_dom_message(message))
            _write_message({"ok": True, "text_id": result.get("text_id")}, stdout)
    finally:
        if hasattr(bus, "close"):
            bus.close()


def main() -> None:
    run_host()


if __name__ == "__main__":
    main()
