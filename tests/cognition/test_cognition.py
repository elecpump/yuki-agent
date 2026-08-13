from yuki.cognition.agent import CognitionAgent
from yuki.config import Config
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakePipeline:
    def warmup_vlm(self):
        pass


def test_cognition_agent_wires_pipeline_and_responder():
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
    )
    agent.setup()
    assert Topics.AWAKE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    agent.teardown()
