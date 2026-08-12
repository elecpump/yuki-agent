from yuki.cognition.topics_ext import TopicsExt


def test_perception_topic_constants():
    assert TopicsExt.SITUATION_UPDATE == "event/perception/situation_update"
    assert TopicsExt.USER_UTTERANCE == "event/perception/user_utterance"
