import pytest

from yuki.cognition.l1 import L1Engine


def test_reply_greeting():
    engine = L1Engine()
    reply = engine.reply("你好")
    assert isinstance(reply, str)
    assert len(reply) > 0


def test_reply_acknowledges_call():
    engine = L1Engine()
    reply = engine.reply("")
    assert reply == "我在，你说。"


def test_reply_with_context_topic():
    engine = L1Engine()
    reply = engine.reply("继续说", context={"topic": "climate"})
    assert reply  # 非空
