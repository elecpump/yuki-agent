from yuki.cognition.brain.sink import TunerSink


class FakeTuner:
    def __init__(self):
        self.calls = []

    def on_proactive_open(self):
        self.calls.append("open")

    def on_user_utterance(self, text):
        self.calls.append(("utter", text))


def test_tuner_sink_forwards_proactive_and_utterance():
    tuner = FakeTuner()
    sink = TunerSink(tuner)

    sink.on_proactive_open()
    sink.on_user_utterance("hello")

    assert tuner.calls == ["open", ("utter", "hello")]
