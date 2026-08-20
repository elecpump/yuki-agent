from typing import Callable

from yuki.cognition.brain.classifier import Intent
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.tuner import FeedbackTuner, detect_polarity
from yuki.memory.manager import MemoryManager

LABEL_FREQUENCY_LOW = "yuki.rhythm.frequency.low"
LABEL_FREQUENCY_HIGH = "yuki.rhythm.frequency.high"
LABEL_LENGTH_SHORT = "yuki.rhythm.length.short"

RHYTHM_CONTENTS = {
    LABEL_FREQUENCY_LOW: "用户不喜欢频繁主动开口",
    LABEL_FREQUENCY_HIGH: "用户喜欢主动互动",
    LABEL_LENGTH_SHORT: "用户希望回复更简短",
}

CORRECTION_KEYWORDS = ("其实我不", "说反了", "我改主意了", "我说错了")
STATEMENT_KEYWORDS = ("我喜欢", "我不喜欢", "我希望", "请", "别", "不要", "讨厌")
LENGTH_KEYWORDS = ("话多", "话太多", "啰嗦", "太长", "简单点", "简短点")


class PreferenceSedimenter:
    """环2 偏好沉淀：重复反馈模式 → 偏好记忆（带置信度），可显式纠正。"""

    def __init__(self, memory: MemoryManager, *, tuner: FeedbackTuner | None = None,
                 min_signals: int = 3, confidence_threshold: float = 0.6,
                 topic_engagement_threshold: int = 3,
                 frequency_floor_s: float = 120.0,
                 on_sedimented: Callable[..., None] | None = None,
                 soul: SoulStore | None = None) -> None:
        self._memory = memory
        self._tuner = tuner
        self._on_sedimented = on_sedimented
        self._soul = soul
        self._min_signals = min_signals
        self._confidence_threshold = confidence_threshold
        self._topic_threshold = topic_engagement_threshold
        self._frequency_floor_s = frequency_floor_s
        self._counts: dict[str, dict] = {}
        self._topics: dict[str, int] = {}
        self._sedimented: set[str] = set()
        self._written_conf: dict[str, float] = {}
        for m in self._memory.list(memory_type="preference"):
            if m.get("source") == "feedback":
                label = m.get("metadata", {}).get("label")
                if label:
                    self._sedimented.add(label)

    def on_user_utterance(self, text: str, intent) -> None:
        text = text or ""
        if self._soul is not None:
            self._soul.apply_core_value_feedback(text)
        if intent == Intent.SYSTEM:
            if any(kw in text for kw in CORRECTION_KEYWORDS):
                self._apply_correction(text)
                return
            if any(kw in text for kw in STATEMENT_KEYWORDS):
                self._write_explicit(text)
                return
        polarity = detect_polarity(text)
        if polarity == "negative":
            self._reinforce(LABEL_FREQUENCY_LOW, LABEL_FREQUENCY_HIGH)
            if any(kw in text for kw in LENGTH_KEYWORDS):
                self._reinforce(LABEL_LENGTH_SHORT, None)
        elif polarity == "positive":
            self._reinforce(LABEL_FREQUENCY_HIGH, LABEL_FREQUENCY_LOW)

    def on_engagement(self, topic: str) -> None:
        if not topic:
            return
        label = f"yuki.topic.{topic}"
        self._topics[label] = self._topics.get(label, 0) + 1
        if self._topics[label] >= self._topic_threshold and label not in self._sedimented:
            self._write_preference(f"对{topic}话题感兴趣", label, source="feedback", confidence=0.8)
            self._sedimented.add(label)

    def _reinforce(self, label: str, opposite: str | None) -> None:
        entry = self._counts.setdefault(label, {"hits": 0, "contradicts": 0})
        entry["hits"] += 1
        if opposite:
            opp = self._counts.setdefault(opposite, {"hits": 0, "contradicts": 0})
            opp["contradicts"] += 1
            self._maybe_sediment(opposite)  # 反向也重新评估
        self._maybe_sediment(label)

    def _maybe_sediment(self, label: str) -> None:
        entry = self._counts[label]
        total = entry["hits"] + entry["contradicts"]
        confidence = entry["hits"] / max(1, total)
        if entry["hits"] >= self._min_signals and confidence >= self._confidence_threshold:
            if confidence > self._written_conf.get(label, 0.0):
                self._write_preference(RHYTHM_CONTENTS[label], label, source="feedback", confidence=confidence)
                self._written_conf[label] = confidence
                self._sedimented.add(label)
                if label == LABEL_FREQUENCY_LOW and self._tuner is not None:
                    self._tuner.set_cooldown_floor(self._frequency_floor_s)
        elif label in self._sedimented and confidence < self._confidence_threshold:
            # 已沉淀但被反向信号拉低 → 降级删除（含跨会话：由持久化 feedback 行种子恢复）
            self._remove_by_label(label)
            self._sedimented.discard(label)
            self._written_conf.pop(label, None)

    def _write_explicit(self, text: str) -> None:
        self._write_preference(
            text,
            "yuki.explicit",
            source="user",
            confidence=1.0,
            sensitivity=0,
        )

    def _apply_correction(self, text: str) -> None:
        # 简化实现（§8.3 显式>隐式）：删全部隐式偏好，写显式纠正偏好
        for m in self._memory.list(memory_type="preference"):
            if m.get("source") == "feedback":
                self._memory.delete(m["id"])
        self._counts = {}
        self._sedimented = set()
        self._written_conf = {}
        self._write_explicit(text)

    def _write_preference(
        self,
        content: str,
        label: str,
        *,
        source: str,
        confidence: float,
        sensitivity: int = 0,
    ) -> None:
        is_new = label == "yuki.explicit" or not any(
            m.get("metadata", {}).get("label") == label
            for m in self._memory.list(memory_type="preference")
        )
        self._remove_by_label(label)
        self._memory.write("preference", content, confidence=confidence, source=source,
                           sensitivity=sensitivity, metadata={"label": label})
        if self._on_sedimented is not None:
            try:
                self._on_sedimented(label=label, confidence=confidence, content=content, is_new=is_new)
            except TypeError:
                self._on_sedimented()

    def _remove_by_label(self, label: str) -> None:
        for m in self._memory.list(memory_type="preference"):
            if m.get("metadata", {}).get("label") == label:
                self._memory.delete(m["id"])
