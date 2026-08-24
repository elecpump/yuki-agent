from typing import TYPE_CHECKING, Protocol

from yuki.cognition.brain.classifier import Intent

if TYPE_CHECKING:
    from yuki.cognition.brain.sedimenter import PreferenceSedimenter
    from yuki.cognition.brain.tuner import FeedbackTuner


class DecisionSink(Protocol):
    """Consumes downstream DecisionHub events."""

    trusted_only: bool

    def on_proactive_open(self) -> None: ...

    def on_user_utterance(self, text: str, intent: Intent | None = None) -> None: ...

    def on_engagement(self, topic: str) -> None: ...


class TunerSink:
    trusted_only = False

    def __init__(self, tuner: "FeedbackTuner") -> None:
        self._tuner = tuner

    def on_proactive_open(self) -> None:
        self._tuner.on_proactive_open()

    def on_user_utterance(self, text: str, intent: Intent | None = None) -> None:
        self._tuner.on_user_utterance(text)

    def on_engagement(self, topic: str) -> None:
        pass


class SedimenterSink:
    trusted_only = True

    def __init__(self, sedimenter: "PreferenceSedimenter") -> None:
        self._sedimenter = sedimenter

    def on_proactive_open(self) -> None:
        pass

    def on_user_utterance(self, text: str, intent: Intent | None = None) -> None:
        if intent is not None:
            self._sedimenter.on_user_utterance(text, intent)

    def on_engagement(self, topic: str) -> None:
        self._sedimenter.on_engagement(topic)
