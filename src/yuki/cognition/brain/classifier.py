from enum import Enum


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    JOY = "joy"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANGER = "anger"
    LOVE = "love"
    TIRED = "tired"


VALID_EMOTION_VALUES = frozenset(emotion.value for emotion in Emotion)
