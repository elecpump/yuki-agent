from tests.fakes import RecordingCallTracker
from yuki.cognition.brain.local.router import GateRoute, LocalRouter


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    def generate(self, messages, **kwargs):
        self.messages.append((messages, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_model_crisis_flag_forces_cloud_and_marks_decision():
    model = FakeModel('{"route":"local","confidence":0.91,"crisis":true}')

    decision = LocalRouter(model, threshold=0.7).route("我不想活了")

    assert decision.route == GateRoute.CLOUD
    assert decision.crisis is True
    assert decision.reason == "crisis"
    assert len(model.messages) == 1


def test_model_judges_emotion_and_polarity():
    model = FakeModel(
        '{"route":"local","confidence":0.91,"emotion":"sadness","polarity":"negative"}'
    )

    decision = LocalRouter(model, threshold=0.7).route("我今天很难过")

    assert decision.route == GateRoute.LOCAL
    assert decision.emotion == "sadness"
    assert decision.polarity == "negative"


def test_missing_signal_fields_default_to_neutral():
    model = FakeModel('{"route":"local","confidence":0.91}')

    decision = LocalRouter(model, threshold=0.7).route("你好")

    assert decision.crisis is False
    assert decision.emotion == "neutral"
    assert decision.polarity == "neutral"


def test_invalid_crisis_type_is_rejected():
    model = FakeModel('{"route":"local","confidence":0.91,"crisis":"true"}')

    decision = LocalRouter(model, retry=0, threshold=0.7).route("你好")

    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "router_failed"


def test_valid_json_routes_simple_chat_local():
    model = FakeModel('{"route":"local","confidence":0.91}')

    decision = LocalRouter(model, threshold=0.7).route("你好")

    assert decision.route == GateRoute.LOCAL
    assert decision.confidence == 0.91
    assert decision.reason == "router"


def test_valid_json_can_route_complex_request_to_cloud():
    model = FakeModel('{"route":"cloud","confidence":0.91}')

    decision = LocalRouter(model, threshold=0.7).route("分析一下这份方案")

    assert decision.route == GateRoute.CLOUD


def test_low_confidence_falls_to_cloud():
    model = FakeModel('{"route":"local","confidence":0.2}')

    decision = LocalRouter(model, threshold=0.7).route("你好")

    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "low_confidence"


def test_invalid_json_retries_then_cloud():
    model = FakeModel("not json", "still not json")

    decision = LocalRouter(model, retry=1).route("你好")

    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "router_failed"
    assert len(model.messages) == 2


def test_router_prompt_requests_signal_fields():
    model = FakeModel('{"route":"local","confidence":0.91}')

    LocalRouter(model).route("你好")

    prompt = str(model.messages[0][0])
    assert '"local"' in prompt
    assert '"cloud"' in prompt
    assert "crisis" in prompt
    assert "emotion" in prompt
    assert "polarity" in prompt
    assert "tool_call" not in prompt
    assert "intent" not in prompt


def test_router_records_call_tracker_metrics():
    tracker = RecordingCallTracker()
    model = FakeModel('{"route":"local","confidence":0.91}')
    router = LocalRouter(model, threshold=0.7, model_registry=tracker)

    assert router.route("hi").route == GateRoute.LOCAL

    assert tracker.success == 1
    assert tracker.failure == 0


def test_router_records_invalid_model_output_as_failure():
    tracker = RecordingCallTracker()
    model = FakeModel("not json")
    router = LocalRouter(model, retry=0, model_registry=tracker)

    assert router.route("hi").route == GateRoute.CLOUD

    assert tracker.success == 0
    assert tracker.failure == 1
