import time

from yuki.cognition.responder import make_reply


def test_make_reply_shape():
    now = time.time()
    reply = make_reply({"source": "hotkey", "ts": now})
    assert set(reply.keys()) == {"text", "ts"}
    assert isinstance(reply["text"], str)
    assert reply["ts"] >= now


def test_make_reply_acknowledges_call():
    reply = make_reply({"source": "hotkey", "ts": 0.0})
    assert reply["text"] == "我在，你说。"
