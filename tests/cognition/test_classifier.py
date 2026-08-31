from yuki.cognition.brain.classifier import VALID_EMOTION_VALUES, Emotion


def test_emotion_enum_covers_expected_spectrum():
    assert {emotion.value for emotion in Emotion} == {
        "neutral",
        "joy",
        "sadness",
        "anxiety",
        "anger",
        "love",
        "tired",
    }


def test_valid_emotion_values_match_enum():
    assert VALID_EMOTION_VALUES == {emotion.value for emotion in Emotion}


def test_emotion_is_str_enum():
    assert Emotion.JOY.value == "joy"
    assert Emotion("sadness") == Emotion.SADNESS
