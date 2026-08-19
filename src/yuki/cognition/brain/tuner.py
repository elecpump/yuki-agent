import time

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import COOLDOWN_KEY, SoulStore, TunerStateStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.tuner")

NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")

NEGATIVE_TRAIT_DELTAS = {"warmth": -0.03, "directness": 0.02}
POSITIVE_TRAIT_DELTAS = {"warmth": 0.03, "humor": 0.02}
ENGAGEMENT_TRAIT_DELTAS = {"proactiveness": 0.02}
TIMEOUT_TRAIT_DELTAS = {"proactiveness": -0.03}


def detect_polarity(text: str) -> str:
    lowered = (text or "").lower()
    if any(kw in lowered for kw in NEGATIVE_KEYWORDS):
        return "negative"
    if any(kw in lowered for kw in POSITIVE_KEYWORDS):
        return "positive"
    return "neutral"


class FeedbackTuner:
    """环1 参数自调：反馈调 cooldown，显式节奏信号调 traits。"""

    def __init__(self, policy: DecisionPolicy, state: TunerStateStore | SoulStore, *,
                 soul: SoulStore | None = None,
                 window_s: float = 90.0, cooldown_min_s: float = 30.0,
                 cooldown_max_s: float = 600.0) -> None:
        self._policy = policy
        if isinstance(state, SoulStore):
            self._state = state.tuner_state
            self._soul = soul if isinstance(soul, SoulStore) else state
        else:
            self._state = state
            self._soul = soul if isinstance(soul, SoulStore) else None
        self._window_s = window_s
        self._min_s = cooldown_min_s
        self._max_s = cooldown_max_s
        self._open_ts = None
        self._cooldown = policy.cooldown_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def load_soul(self) -> None:
        params = self._state.load()
        if params and isinstance(params.get(COOLDOWN_KEY), (int, float)):
            self._cooldown = min(max(float(params[COOLDOWN_KEY]), self._min_s), self._max_s)
            self._policy.set_cooldown_s(self._cooldown)

    def on_proactive_open(self) -> None:
        self._open_ts = time.time()

    def on_user_utterance(self, text: str) -> None:
        self._check_timeout()
        polarity = detect_polarity(text)
        if polarity == "negative":
            self._adjust_traits(NEGATIVE_TRAIT_DELTAS)
            self.adjust(1.5)
            self._open_ts = None
            return
        if polarity == "positive":
            self._adjust_traits(POSITIVE_TRAIT_DELTAS)
            self.adjust(0.8)
            self._open_ts = None
            return
        if self._open_ts is not None and time.time() - self._open_ts <= self._window_s:
            self._adjust_traits(ENGAGEMENT_TRAIT_DELTAS)
            self.adjust(0.9)
            self._open_ts = None

    def _check_timeout(self) -> None:
        if self._open_ts is not None and time.time() - self._open_ts > self._window_s:
            self._adjust_traits(TIMEOUT_TRAIT_DELTAS)
            self.adjust(1.3)
            self._open_ts = None

    def adjust(self, factor: float) -> None:
        new = min(max(self._cooldown * factor, self._min_s), self._max_s)
        if new == self._cooldown:
            return
        self._cooldown = new
        self._policy.set_cooldown_s(new)
        self._state.save({COOLDOWN_KEY: new})
        logger.info("tuned cooldown", cooldown_s=new, factor=factor)

    def set_cooldown_floor(self, value: float) -> None:
        self._min_s = min(max(self._min_s, value), self._max_s)
        if self._cooldown < self._min_s:
            self._cooldown = self._min_s
            self._policy.set_cooldown_s(self._cooldown)
            self._state.save({COOLDOWN_KEY: self._cooldown})

    def _adjust_traits(self, deltas: dict[str, float]) -> None:
        if self._soul is not None:
            self._soul.adjust_traits(deltas)
