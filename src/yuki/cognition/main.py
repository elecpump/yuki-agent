from typing import Callable

from yuki.bus import BusNode
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.responder import make_reply
from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.topics import Topics


def build_cognition(bus: BusNode, *, pipeline=None) -> None:
    if pipeline is not None:
        return

    def on_awake(topic: str, payload: dict) -> None:
        bus.publish(Topics.REPLY, make_reply(payload))

    bus.subscribe(Topics.AWAKE, on_awake)


def main() -> None:
    config = Config.from_env()
    bus = BusNode(base_port=config.base_port, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    pipeline = build_pipeline(bus)
    pipeline._vlm.warmup()  # VLM 后台预热（不可用则降级文本模式）
    build_l1_responder(bus)
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
