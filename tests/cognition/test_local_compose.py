from yuki.cognition.brain.local.compose import LocalComposer, LocalViewBuilder
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


class FakeModel:
    def __init__(self):
        self.messages = []

    def generate(self, messages, **kwargs):
        self.messages.append((messages, kwargs))
        return "本地回答"


def test_local_composer_generates_short_reply(tmp_path):
    model = FakeModel()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    memory.write("preference", "喜欢简短回答", sensitivity=1)
    composer = LocalComposer(model, view_builder=LocalViewBuilder(max_tokens=600))
    reply = composer.generate("请回答简短一点", ContextSnapshot(), memory)
    assert reply == "本地回答"
    assert "喜欢简短回答" in model.messages[0][0][1]["content"]
    memory.close()


def test_local_view_builder_drops_crisis_history_turn():
    view = LocalViewBuilder(max_tokens=1000).build(
        ContextSnapshot(recent_turns=(
            {"kind": "user", "content": "我不想活了"},
            {"kind": "agent", "content": "普通回复"},
        )),
        None,
        "继续聊",
    )
    assert "我不想活了" not in view
    assert "普通回复" in view


def test_local_view_builder_respects_budget():
    view = LocalViewBuilder(max_tokens=20).build(
        ContextSnapshot(recent_turns=tuple(
            {"kind": "user", "content": "很长的旧对话" * 100} for _ in range(5)
        )),
        None,
        "短问题",
    )
    assert len(view) < 200


def test_local_view_builder_never_drops_current_utterance_when_budget_is_full():
    view = LocalViewBuilder(max_tokens=20).build(
        ContextSnapshot(
            situation={"topic": "很长情境" * 100},
            recent_turns=(
                {"kind": "user", "content": "很长旧对话" * 100},
            ),
        ),
        None,
        "CURRENT_QUESTION_SHOULD_SURVIVE" * 10,
    )

    assert "CURRENT_QUESTION" in view
