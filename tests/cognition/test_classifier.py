import pytest

from yuki.cognition.brain.classifier import (
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)


def test_default_intent_rules_hit():
    clf = RuleIntentClassifier()
    assert clf.classify("你好，在吗") == Intent.CHIT_CHAT
    assert clf.classify("我今天很难过") == Intent.EMOTIONAL
    assert clf.classify("讲个笑话给我听") == Intent.ENTERTAINMENT
    assert clf.classify("成语接龙来不来") == Intent.GAME
    assert clf.classify("扮演我的女朋友") == Intent.ROLEPLAY
    assert clf.classify("帮我写首情诗") == Intent.CREATIVE
    assert clf.classify("睡不着陪我聊聊") == Intent.COMPANION
    assert clf.classify("你能做什么") == Intent.SYSTEM
    assert clf.classify("今天天气怎么样") == Intent.CHIT_CHAT


def test_safety_wins_over_other_intents():
    clf = RuleIntentClassifier()
    assert clf.classify("我很难过，不想活了") == Intent.SAFETY


def test_unknown_fallback():
    clf = RuleIntentClassifier()
    assert clf.classify("qwertyuiop 乱码") == Intent.UNKNOWN
    assert clf.classify("") == Intent.UNKNOWN


def test_case_insensitive_and_injectable_rules():
    clf = RuleIntentClassifier(rules=[(("HELLO",), Intent.CHIT_CHAT)])
    assert clf.classify("Say HELLO world") == Intent.CHIT_CHAT


def test_emotion_classifier():
    clf = RuleEmotionClassifier()
    assert clf.classify("太开心了") == Emotion.JOY
    assert clf.classify("我今天很难过") == Emotion.SADNESS
    assert clf.classify("压力好大") == Emotion.ANXIETY
    assert clf.classify("随便聊聊") == Emotion.NEUTRAL
