from yuki.cognition.brain.local.router import GateRoute, LocalRouter
from yuki.cognition.model_registry import ModelRegistry, ModelSpec


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


def test_crisis_short_circuits_to_cloud_before_model():
    model = FakeModel(RuntimeError("should not run"))

    decision = LocalRouter(model).route("我不想活了")

    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "crisis"
    assert model.messages == []


def test_explicit_preference_short_circuits_to_cloud_before_model():
    model = FakeModel(RuntimeError("should not run"))

    decision = LocalRouter(model).route("请记住我喜欢黑咖啡")

    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "explicit_preference"
    assert model.messages == []


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


def test_router_prompt_only_requests_binary_gate_fields():
    model = FakeModel('{"route":"local","confidence":0.91}')

    LocalRouter(model).route("你好")

    prompt = str(model.messages[0][0])
    assert '"local"' in prompt
    assert '"cloud"' in prompt
    assert "tool_call" not in prompt
    assert "emotion" not in prompt
    assert "intent" not in prompt


def test_router_records_model_registry_metrics():
    registry = ModelRegistry()
    registry.register(ModelSpec(name="local_chat", loader=lambda: object()))
    model = FakeModel('{"route":"local","confidence":0.91}')
    router = LocalRouter(model, threshold=0.7, model_registry=registry)

    assert router.route("hi").route == GateRoute.LOCAL

    health = registry.get_model_health("local_chat")
    assert health["success_count"] == 1
    assert health["failure_count"] == 0


def test_router_records_invalid_model_output_as_failure():
    registry = ModelRegistry()
    registry.register(ModelSpec(name="local_chat", loader=lambda: object()))
    model = FakeModel("not json")
    router = LocalRouter(model, retry=0, model_registry=registry)

    assert router.route("hi").route == GateRoute.CLOUD

    health = registry.get_model_health("local_chat")
    assert health["success_count"] == 0
    assert health["failure_count"] == 1
