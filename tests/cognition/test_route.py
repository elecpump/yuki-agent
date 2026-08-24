from yuki.cognition.brain.local.router import LocalRoute, RouterDecision
from yuki.cognition.brain.route import DecisionRouter
from yuki.cognition.context.snapshot import ContextSnapshot


class FakeDispatcher:
    def __init__(self, route, result):
        self._route = route
        self._result = result
        self.calls = []

    def can_handle(self, decision):
        return decision.route == self._route

    def dispatch(self, text, snapshot, decision):
        self.calls.append((text, decision))
        return self._result


class FallbackDispatcher:
    def can_handle(self, decision):
        return True

    def dispatch(self, text, snapshot, decision):
        return {"rendered": "fallback", "route": "fallback"}


def test_router_dispatches_to_first_matching():
    a = FakeDispatcher(LocalRoute.CHAT_LOCAL, {"rendered": "a"})
    b = FakeDispatcher(LocalRoute.VISION, {"rendered": "b"})
    router = DecisionRouter()
    router.register(a)
    router.register(b)
    decision = RouterDecision(LocalRoute.CHAT_LOCAL, 0.9)
    result = router.dispatch("hi", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["rendered"] == "a"
    assert a.calls == [("hi", decision)]


def test_router_falls_back_when_no_dispatcher_matches():
    router = DecisionRouter()
    decision = RouterDecision(LocalRoute.CLOUD, 0.1)
    result = router.dispatch("x", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["route"] == "fallback"


def test_router_returns_first_match_even_with_later_matches():
    d = FakeDispatcher(LocalRoute.CLOUD, {"rendered": "first"})
    e = FakeDispatcher(LocalRoute.CLOUD, {"rendered": "second"})
    router = DecisionRouter()
    router.register(d)
    router.register(e)
    decision = RouterDecision(LocalRoute.CLOUD, 0.8)
    result = router.dispatch("x", ContextSnapshot(), decision, fallback=FallbackDispatcher())
    assert result["rendered"] == "first"
    assert len(d.calls) == 1
    assert len(e.calls) == 0
