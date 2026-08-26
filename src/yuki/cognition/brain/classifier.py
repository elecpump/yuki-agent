from enum import Enum


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    JOY = "joy"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANGER = "anger"
    LOVE = "love"
    TIRED = "tired"


EMOTION_KEYWORDS: dict[Emotion, tuple[str, ...]] = {
    Emotion.JOY: ("开心", "高兴", "好棒", "太棒了", "哈哈", "真棒", "耶"),
    Emotion.SADNESS: ("难过", "伤心", "想哭", "委屈", "失落", "哭了"),
    Emotion.ANXIETY: ("焦虑", "紧张", "压力", "害怕", "担心", "不安"),
    Emotion.ANGER: ("生气", "气死", "烦死了", "讨厌", "恼火"),
    Emotion.LOVE: ("想你", "爱你", "喜欢你", "抱抱"),
    Emotion.TIRED: ("好累", "累死", "疲惫", "困"),
}


def detect_emotion(text: str) -> Emotion:
    lowered = (text or "").lower()
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return emotion
    return Emotion.NEUTRAL

