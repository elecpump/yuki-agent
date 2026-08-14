import time

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.tuner")

NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")

COOLDOWN_KEY = "proactive_cooldown_s"


class FeedbackTuner:
    """环1 参数自调：隐式回应 + 显式话语 → 调整主动开口冷却，持久化到 soul。"""

    def __init__(self, policy: DecisionPolicy, soul: SoulStore, *,
                 window_s: float = 90.0, cooldown_min_s: float = 30.0,
                 cooldown_max_s: float = 600.0) -> None:
        self._policy = policy
        self._soul = soul
        self._window_s = window_s
        self._min_s = cooldown_min_s
        self._max_s = cooldown_max_s
        self._open_ts = None
        self._cooldown = policy.cooldown_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def load_soul(self) -> None:
        params = self._soul.load()
        if params and isinstance(params.get(COOLDOWN_KEY), (int, float)):
            self._cooldown = min(max(float(params[COOLDOWN_KEY]), self._min_s), self._max_s)
            self._policy.set_cooldown_s(self._cooldown)

    def on_proactive_open(self) -> None:
        self._open_ts = time.time()

    def on_user_utterance(self, text: str) -> None:
        self._check_timeout()
        lowered = (text or "").lower()
        if any(kw in lowered for kw in NEGATIVE_KEYWORDS):
            self.adjust(1.5)
            self._open_ts = None
            return
        if any(kw in lowered for kw in POSITIVE_KEYWORDS):
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

    def adjust(self, factor: float) -> None:
        new = min(max(self._cooldown * factor, self._min_s), self._max_s)
        if new == self._cooldown:
            return
        self._cooldown = new
        self._policy.set_cooldown_s(new)
        self._soul.save({COOLDOWN_KEY: new})
        logger.info("tuned cooldown", cooldown_s=new, factor=factor)
