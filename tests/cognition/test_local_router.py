from pydantic import BaseModel

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.brain.local.router import LocalRoute, LocalRouter
from yuki.functions.registry import FunctionRegistry


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


class EchoParams(BaseModel):
    text: str


def make_registry():
    registry = FunctionRegistry()

    @registry.tool("local.echo", description="echo text", params=EchoParams)
    def echo(params):
        return {"reply": params.text}

    return registry


def test_crisis_short_circuits_before_model():
    model = FakeModel(RuntimeError("should not run"))
    router = LocalRouter(model)
    decision = router.route("我不想活了")
    assert decision.route == LocalRoute.CLOUD
    assert decision.intent == Intent.SAFETY
    assert model.messages == []


def test_valid_json_routes_chat_local():
    model = FakeModel(
        '{"route":"chat_local","confidence":0.91,"intent":"chit_chat","emotion":"neutral",'
        '"tool_call":null}'
    )
    router = LocalRouter(model, threshold=0.7)
    decision = router.route("你好")
    assert decision.route == LocalRoute.CHAT_LOCAL
    assert decision.intent == Intent.CHIT_CHAT
    assert decision.emotion == Emotion.NEUTRAL
    assert decision.trusted_metadata is True


def test_low_confidence_falls_to_cloud():
    model = FakeModel(
        '{"route":"chat_local","confidence":0.2,"intent":"chit_chat","emotion":"neutral",'
        '"tool_call":null}'
    )
    router = LocalRouter(model, threshold=0.7)
    decision = router.route("你好")
    assert decision.route == LocalRoute.CLOUD
    assert decision.trusted_metadata is False


def test_invalid_json_retries_then_cloud():
    model = FakeModel("not json", "still not json")
    router = LocalRouter(model, retry=1)
    decision = router.route("你好")
    assert decision.route == LocalRoute.CLOUD
    assert len(model.messages) == 2


def test_tool_local_requires_allowlisted_tool_call():
    registry = make_registry()
    model = FakeModel(
        '{"route":"tool_local","confidence":0.9,"intent":"system","emotion":"neutral",'
        '"tool_call":{"name":"local.echo","arguments":{"text":"ok"}}}'
    )
    router = LocalRouter(
        model,
        registry=registry,
        local_tool_allowlist=["local.echo"],
    )
    decision = router.route("echo")
    assert decision.route == LocalRoute.TOOL_LOCAL
    assert decision.tool_call["name"] == "local.echo"


def test_tool_local_rejects_non_allowlisted_tool():
    registry = make_registry()
    model = FakeModel(
        '{"route":"tool_local","confidence":0.9,"intent":"system","emotion":"neutral",'
        '"tool_call":{"name":"local.echo","arguments":{"text":"ok"}}}'
    )
    router = LocalRouter(model, registry=registry, local_tool_allowlist=[])
    assert router.route("echo").route == LocalRoute.CLOUD
