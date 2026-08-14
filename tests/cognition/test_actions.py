import pytest

from yuki.cognition.brain.actions import ACTION_EXECUTORS, Action, ActionContext
from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


class FakeL1:
    def reply(self, text, context=None):
        return f"l1:{text}" if text else "我在，你说。"


@pytest.fixture()
def ctx():
    return ActionContext(intent=Intent.UNKNOWN, emotion=Emotion.NEUTRAL, text="你好", situation=None)


def test_all_action_names_have_executors():
    expected = {
        "empathize", "acknowledge", "comfort", "encourage", "ask", "clarify",
        "inform", "joke", "story", "invite_game", "farewell",
        "safety_escalate", "write_memory", "call_function", "stay_silent",
    }
    assert set(ACTION_EXECUTORS) == expected


def test_empathize_uses_emotion(ctx):
    ctx.emotion = Emotion.SADNESS
    assert ACTION_EXECUTORS["empathize"](Action("empathize"), ctx) != ""
    ctx.emotion = Emotion.JOY
    assert "开心" in ACTION_EXECUTORS["empathize"](Action("empathize"), ctx)


def test_ask_injects_situation_topic():
    c = ActionContext(intent=Intent.UNKNOWN, emotion=Emotion.NEUTRAL, text="",
                      situation={"topic": "量子计算"})
    assert "量子计算" in ACTION_EXECUTORS["ask"](Action("ask"), c)


def test_inform_uses_l1():
    c = ActionContext(intent=Intent.CHIT_CHAT, emotion=Emotion.NEUTRAL, text="你好", l1=FakeL1())
    assert ACTION_EXECUTORS["inform"](Action("inform"), c) == "l1:你好"


def test_joke_falls_back_to_placeholder():
    out = ACTION_EXECUTORS["joke"](Action("joke", {"items": []}), ctx)
    assert "还在学" in out


def test_write_memory_is_side_effect_only(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    c = ActionContext(intent=Intent.EMOTIONAL, emotion=Emotion.SADNESS, text="我今天升职了",
                      memory=memory)
    text = ACTION_EXECUTORS["write_memory"](Action("write_memory", {
        "memory_type": "preference", "content": "我今天升职了"}), c)
    assert text == ""
    results = memory.query("升职")
    assert results and results[0]["content"] == "我今天升职了"


def test_call_function_dispatches_when_registry_present():
    registry = FunctionRegistry()
    registry.tool("echo", description="e", params=None)(lambda p: "ok")
    c = ActionContext(intent=Intent.SYSTEM, emotion=Emotion.NEUTRAL, text="",
                      registry=registry)
    text = ACTION_EXECUTORS["call_function"](Action("call_function", {
        "name": "echo", "arguments": {}}), c)
    assert text == ""


def test_stay_silent_no_text(ctx):
    assert ACTION_EXECUTORS["stay_silent"](Action("stay_silent"), ctx) == ""


def test_safety_escalate_mentions_help(ctx):
    out = ACTION_EXECUTORS["safety_escalate"](Action("safety_escalate"), ctx)
    assert "求助" in out
