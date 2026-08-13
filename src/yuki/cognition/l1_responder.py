import time

from yuki.cognition.l1 import L1Engine
from yuki.logger import get_logger
from yuki.payloads import SituationUpdatePayload
from yuki.topics import Topics

logger = get_logger("yuki.cognition.l1_responder")


class L1Responder:
    """L1 快答消费者：订阅感知事件 + awake，产出 event/reply。

    职责边界：感知管线只产理解事件；本组件消费并回复。
    SITUATION_UPDATE 仅作为 context 注入，不自动回复——
    主动评论（是否开口/何时开口）留待 Brain 阶段决策。  # TODO(Brain): 主动评论决策
    Brain 阶段直接替换本组件（同样的订阅，更聪明的 Brain）。
    """

    def __init__(self, l1: L1Engine, bus) -> None:
        self._l1 = l1
        self._bus = bus
        self._context: SituationUpdatePayload | None = None

    def on_situation_update(self, topic: str, payload: dict) -> None:
        self._context = payload

    def on_awake(self, topic: str, payload: dict) -> None:
        reply = self._l1.reply("", context=self._context)
        self._publish(reply)

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        reply = self._l1.reply(text, context=self._context)
        self._publish(reply)

    def _publish(self, text: str) -> None:
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_l1_responder(bus, *, l1=None) -> L1Responder:
    responder = L1Responder(l1=l1 or L1Engine(), bus=bus)
    bus.subscribe(Topics.AWAKE, responder.on_awake)
    bus.subscribe(Topics.USER_UTTERANCE, responder.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, responder.on_situation_update)
    return responder
