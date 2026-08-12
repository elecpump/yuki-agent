import time

from yuki.cognition.l1 import L1Engine
from yuki.cognition.topics_ext import TopicsExt
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.l1_responder")


class L1Responder:
    """L1 快答消费者：订阅感知事件 + awake，产出 event/reply。

    职责边界：感知管线只产理解事件；本组件消费并回复。
    Brain 阶段直接替换本组件（同样的订阅，更聪明的 Brain）。
    """

    def __init__(self, l1: L1Engine, bus) -> None:
        self._l1 = l1
        self._bus = bus

    def on_awake(self, topic: str, payload: dict) -> None:
        reply = self._l1.reply("")
        self._publish(reply)

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        reply = self._l1.reply(text)
        self._publish(reply)

    def _publish(self, text: str) -> None:
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_l1_responder(bus, *, l1=None) -> L1Responder:
    responder = L1Responder(l1=l1 or L1Engine(), bus=bus)
    bus.subscribe(Topics.AWAKE, responder.on_awake)
    bus.subscribe(TopicsExt.USER_UTTERANCE, responder.on_user_utterance)
    return responder
