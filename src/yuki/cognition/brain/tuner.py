"""Feedback-based tuning for proactive reply cooldown."""

import time

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import COOLDOWN_KEY, FLOOR_KEY, TunerStateStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.tuner")

NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")


def detect_polarity(text: str) -> str:
    lowered = (text or "").lower()
    if any(keyword in lowered for keyword in NEGATIVE_KEYWORDS):
        return "negative"
    if any(keyword in lowered for keyword in POSITIVE_KEYWORDS):
        return "positive"
    return "neutral"


class FeedbackTuner:
    """Tune only proactive cooldown; persona traits are not mutated here."""

    def __init__(
        self,
        policy: DecisionPolicy,
        state: TunerStateStore,
        *,
        window_s: float = 90.0,
        cooldown_min_s: float = 30.0,
        cooldown_max_s: float = 600.0,
        floor_step_s: float = 30.0,
        floor_negatives: int = 3,
    ) -> None:
        self._policy = policy
        self._state = state
        self._window_s = window_s
        self._min_s = cooldown_min_s
        self._max_s = cooldown_max_s
        self._floor_step_s = floor_step_s
        self._floor_negatives = max(1, floor_negatives)
        self._open_ts: float | None = None
        self._negatives = 0
        self._cooldown = policy.cooldown_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def load_soul(self) -> None:
        params = self._state.load()
        if not params:
            return
        floor = params.get(FLOOR_KEY)
        if isinstance(floor, (int, float)):
            self._min_s = min(max(float(floor), self._min_s), self._max_s)
        restored = params.get(COOLDOWN_KEY, self._cooldown)
        if isinstance(restored, (int, float)):
            self._cooldown = min(max(float(restored), self._min_s), self._max_s)
            self._policy.set_cooldown_s(self._cooldown)

    def on_proactive_open(self) -> None:
        self._open_ts = time.time()

    def on_user_utterance(self, text: str) -> None:
        self._check_timeout()
        polarity = detect_polarity(text)
        if polarity == "negative":
            self._negatives += 1
            self.adjust(1.5)
            if self._negatives >= self._floor_negatives:
                self._negatives = 0
                self._raise_floor()
            self._open_ts = None
            return
        if polarity == "positive":
            self._negatives = 0
            self.adjust(0.8)
            self._open_ts = None
            return
        if self._open_ts is not None and time.time() - self._open_ts <= self._window_s:
            self.adjust(0.9)
            self._open_ts = None

    def _check_timeout(self) -> None:
        if self._open_ts is not None and time.time() - self._open_ts > self._window_s:
            self.adjust(1.3)
            self._open_ts = None

    def _raise_floor(self) -> None:
        self._min_s = min(self._min_s + self._floor_step_s, self._max_s)
        if self._cooldown < self._min_s:
            self._cooldown = self._min_s
            self._policy.set_cooldown_s(self._cooldown)
        self._state.save({COOLDOWN_KEY: self._cooldown, FLOOR_KEY: self._min_s})

    def adjust(self, factor: float) -> None:
        new = min(max(self._cooldown * factor, self._min_s), self._max_s)
        if new == self._cooldown:
            return
        self._cooldown = new
        self._policy.set_cooldown_s(new)
        self._state.save({COOLDOWN_KEY: new})
        logger.info("tuned cooldown", cooldown_s=new, factor=factor)
