from typing import Protocol

from yuki.cognition.brain.local.router import RouterDecision
from yuki.cognition.context.snapshot import ContextSnapshot


class RouteDispatcher(Protocol):
    """Handles a class of RouterDecision."""

    def can_handle(self, decision: RouterDecision) -> bool: ...

    def dispatch(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
    ) -> dict: ...


class DecisionRouter:
    """Dispatch to the first registered route handler, otherwise fallback."""

    def __init__(self) -> None:
        self._dispatchers: list[RouteDispatcher] = []

    def register(self, dispatcher: RouteDispatcher) -> None:
        self._dispatchers.append(dispatcher)

    def dispatch(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
        *,
        fallback: RouteDispatcher,
    ) -> dict:
        for dispatcher in self._dispatchers:
            if dispatcher.can_handle(decision):
                return dispatcher.dispatch(text, snapshot, decision)
        return fallback.dispatch(text, snapshot, decision)
