from yuki.cognition.brain.classifier import Intent
from yuki.cognition.brain.sink import SedimenterSink, TunerSink


class FakeTuner:
    def __init__(self):
        self.calls = []

    def on_proactive_open(self):
        self.calls.append("open")

    def on_user_utterance(self, text):
        self.calls.append(("utter", text))


class FakeSedimenter:
    def __init__(self):
        self.calls = []

    def on_user_utterance(self, text, intent):
        self.calls.append(("utter", text, intent))

    def on_engagement(self, topic):
        self.calls.append(("engage", topic))


def test_tuner_sink_forwards_proactive_and_utterance():
    tuner = FakeTuner()
    sink = TunerSink(tuner)
    sink.on_proactive_open()
    sink.on_user_utterance("hello")
    assert tuner.calls == ["open", ("utter", "hello")]


def test_sedimenter_sink_forwards_utterance_with_intent_and_engagement():
    sed = FakeSedimenter()
    sink = SedimenterSink(sed)
    sink.on_user_utterance("good", Intent.SYSTEM)
    sink.on_engagement("quantum")
    assert sed.calls == [("utter", "good", Intent.SYSTEM), ("engage", "quantum")]


def test_sedimenter_sink_ignores_untrusted_utterance_without_intent():
    sed = FakeSedimenter()
    sink = SedimenterSink(sed)
    sink.on_user_utterance("good")
    assert sed.calls == []
