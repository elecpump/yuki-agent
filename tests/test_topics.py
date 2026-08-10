from yuki.topics import Topics


def test_topic_constants():
    assert Topics.AWAKE == "event/awake"
    assert Topics.REPLY == "event/reply"
    assert Topics.FOCUS_CHANGED == "event/focus_changed"
    assert Topics.MIC == "audio/mic"
    assert Topics.TTS_REF == "audio/tts_ref"
