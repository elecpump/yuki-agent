from yuki.cognition.brain.classifier import Emotion, detect_emotion


def test_detect_emotion_from_user_language():
    assert detect_emotion("太开心了") == Emotion.JOY
    assert detect_emotion("我今天很难过") == Emotion.SADNESS
    assert detect_emotion("压力好大") == Emotion.ANXIETY
    assert detect_emotion("气死我了") == Emotion.ANGER
    assert detect_emotion("想你了") == Emotion.LOVE
    assert detect_emotion("好累") == Emotion.TIRED


def test_detect_emotion_defaults_to_neutral():
    assert detect_emotion("随便聊聊") == Emotion.NEUTRAL
    assert detect_emotion("") == Emotion.NEUTRAL
