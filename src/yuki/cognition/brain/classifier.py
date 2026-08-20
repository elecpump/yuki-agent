from enum import Enum


class Intent(str, Enum):
    CHIT_CHAT = "chit_chat"
    EMOTIONAL = "emotional"
    ENTERTAINMENT = "entertainment"
    GAME = "game"
    ROLEPLAY = "roleplay"
    CREATIVE = "creative"
    COMPANION = "companion"
    SYSTEM = "system"
    SAFETY = "safety"
    UNKNOWN = "unknown"


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    JOY = "joy"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANGER = "anger"
    LOVE = "love"
    TIRED = "tired"

