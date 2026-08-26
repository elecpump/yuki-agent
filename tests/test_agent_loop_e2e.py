import json
import time

import pytest

from tests.fakes import FakeBus
from yuki.bus_server.gateway import GatewayRuntime
from yuki.cognition.brain.hub import DecisionHub
from yuki.config import Config
from yuki.interaction.agent import InteractionAgent
from yuki.topics import Topics


class DispatchingRecordingBus(FakeBus):
    def __init__(self, events_path):
        super().__init__()
        self._events_path = events_path
        self._events_path.parent.mkdir(parents=True)

    def publish(self, topic, payload):
        super().publish(topic, payload)
        event = {"ts": time.time(), "topic": topic, "payload": payload}
        with self._events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        for prefix, handlers in list(self.subscriptions.items()):
            if topic.startswith(prefix):
                for handler in list(handlers):
                    handler(topic, payload)


class FakeHotkeys:
    def register(self, name, handler):
        pass


class RecordingTTS:
    def __init__(self):
        self.spoken = []
        self.cancelled = []

    def speak(self, text, emotion="neutral", *, kind="final", reply_id=None):
        self.spoken.append((text, kind, reply_id))

    def cancel(self, reply_id):
        self.cancelled.append(reply_id)


class InterruptedThenFinalLoop:
    def __init__(self):
        self._calls = 0

    def run(
        self,
        text,
        snapshot,
        memory,
        *,
        crisis=False,
        on_transition=None,
        interrupt_check=None,
    ):
        self._calls += 1
        if on_transition is not None:
            on_transition("让我想想。")
        if self._calls == 1:
            return {"text": "", "interrupted": True, "failed": False}
        return {"text": "这是最终回答。", "interrupted": False, "failed": False}


@pytest.mark.e2e
def test_interrupted_transition_is_cancelled_and_only_final_enters_history(tmp_path):
    session_id = "agent-loop"
    history_dir = tmp_path / "recordings"
    events_path = history_dir / session_id / "events.jsonl"
    bus = DispatchingRecordingBus(events_path)
    tts = RecordingTTS()
    interaction = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    interaction.setup()
    hub = DecisionHub(bus, loop=InterruptedThenFinalLoop(), local_enabled=False)

    first = {"text": "第一个问题", "ts": time.time()}
    bus.publish(Topics.USER_UTTERANCE, first)
    hub.on_user_utterance(Topics.USER_UTTERANCE, first)

    second = {"text": "第二个问题", "ts": time.time()}
    bus.publish(Topics.USER_UTTERANCE, second)
    hub.on_user_utterance(Topics.USER_UTTERANCE, second)

    replies = [payload for topic, payload in bus.published if topic == Topics.REPLY]
    assert [reply["kind"] for reply in replies] == [
        "transition",
        "cancel",
        "transition",
        "final",
    ]
    assert replies[0]["reply_id"] == replies[1]["reply_id"]
    assert replies[2]["reply_id"] == replies[3]["reply_id"]
    assert replies[0]["reply_id"] != replies[2]["reply_id"]
    assert tts.cancelled == [replies[0]["reply_id"]]
    assert tts.spoken == [
        ("让我想想。", "transition", replies[0]["reply_id"]),
        ("让我想想。", "transition", replies[2]["reply_id"]),
        ("这是最终回答。", "final", replies[3]["reply_id"]),
    ]

    runtime = GatewayRuntime(
        Config(gateway={"history_dir": str(history_dir)}),
        bus,
    )
    assert runtime.read_history(session_id)["turns"] == [
        {"role": "user", "text": "第一个问题", "ts": first["ts"]},
        {"role": "user", "text": "第二个问题", "ts": second["ts"]},
        {"role": "assistant", "text": "这是最终回答。", "ts": replies[3]["ts"]},
    ]
