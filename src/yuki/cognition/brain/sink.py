"""Downstream consumers of DecisionHub events."""

from typing import Protocol


class DecisionSink(Protocol):
    def on_proactive_open(self) -> None: ...

    def on_user_utterance(self, text: str) -> None: ...


class TunerSink:
    def __init__(self, tuner) -> None:
        self._tuner = tuner

    def on_proactive_open(self) -> None:
        self._tuner.on_proactive_open()

    def on_user_utterance(self, text: str) -> None:
        self._tuner.on_user_utterance(text)
