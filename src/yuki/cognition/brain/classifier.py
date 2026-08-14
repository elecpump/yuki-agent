from enum import Enum
from typing import Protocol


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


class IntentClassifier(Protocol):
    def classify(self, text: str) -> Intent: ...


class EmotionClassifier(Protocol):
    def classify(self, text: str) -> Emotion: ...


# 顺序即优先级：SAFETY 最先。关键词为子串匹配（文本已小写化）。
DEFAULT_INTENT_RULES: list[tuple[tuple[str, ...], Intent]] = [
    (("自杀", "自伤", "不想活", "想死", "活着没意思", "想结束生命", "割腕"), Intent.SAFETY),
    (("难过", "伤心", "求安慰", "安慰我", "好累", "压力", "焦虑", "失眠", "想哭", "委屈", "孤独", "好烦", "崩溃", "想你了", "抱抱", "升职", "考上"), Intent.EMOTIONAL),
    (("讲笑话", "笑话", "睡前故事", "讲故事", "谜语", "脑筋急转弯", "冷知识", "推荐首歌", "推荐一首歌", "好剧", "电影", "运势", "星座"), Intent.ENTERTAINMENT),
    (("成语接龙", "词语接龙", "海龟汤", "猜数字", "石头剪刀布", "真心话", "大冒险", "剧本杀", "扮演侦探", "井字棋"), Intent.GAME),
    (("扮演", "你是哈利波特", "当我的", "假装你"), Intent.ROLEPLAY),
    (("写首", "写诗", "写词", "续写", "编一个", "起名", "想个"), Intent.CREATIVE),
    (("陪我", "睡不着", "提醒我", "一起学习", "生日", "圣诞", "纪念日"), Intent.COMPANION),
    (("你能做什么", "你会什么", "温柔一点", "凶一点", "回答得", "再见", "晚安", "拜拜", "下次聊", "投诉"), Intent.SYSTEM),
    (("你好", "您好", "在吗", "早上好", "下午好", "晚上好", "嗨", "哈喽", "你叫什么", "你多大了", "在干嘛", "天气", "聊聊", "最近"), Intent.CHIT_CHAT),
]


class RuleIntentClassifier:
    def __init__(self, rules: list[tuple[tuple[str, ...], Intent]] | None = None) -> None:
        self._rules = rules if rules is not None else DEFAULT_INTENT_RULES

    def classify(self, text: str) -> Intent:
        lowered = (text or "").lower()
        for keywords, intent in self._rules:
            if any(kw.lower() in lowered for kw in keywords):
                return intent
        return Intent.UNKNOWN


DEFAULT_EMOTION_RULES: list[tuple[tuple[str, ...], Emotion]] = [
    (("开心", "高兴", "好棒", "太棒了", "升职", "考上", "哈哈", "真棒", "耶"), Emotion.JOY),
    (("难过", "伤心", "想哭", "委屈", "失落", "哭了"), Emotion.SADNESS),
    (("焦虑", "紧张", "压力", "害怕", "担心", "不安"), Emotion.ANXIETY),
    (("生气", "气死", "烦死了", "讨厌", "恼火"), Emotion.ANGER),
    (("想你", "爱你", "喜欢你", "抱抱"), Emotion.LOVE),
    (("好累", "累死", "疲惫", "困"), Emotion.TIRED),
]


class RuleEmotionClassifier:
    def __init__(self, rules: list[tuple[tuple[str, ...], Emotion]] | None = None) -> None:
        self._rules = rules if rules is not None else DEFAULT_EMOTION_RULES

    def classify(self, text: str) -> Emotion:
        lowered = (text or "").lower()
        for keywords, emotion in self._rules:
            if any(kw.lower() in lowered for kw in keywords):
                return emotion
        return Emotion.NEUTRAL
