from yuki.cognition.context.store import ThreadTurnStore
from yuki.cognition.context.working import WorkingContext


def test_add_turns_and_situation(tmp_path):
    context = WorkingContext(ThreadTurnStore(tmp_path / "memory.db"))
    try:
        context.update_situation({"topic": "量子计算", "sensitive": False})
        user_turn_id = context.add_user("你好")
        context.add_agent("我在", reply_to_turn_id=user_turn_id)

        assert context.situation()["topic"] == "量子计算"
        assert context.turn_count() == 2
        items = context.items()
        assert [item["content"] for item in items] == ["我在", "你好"]
        assert [item["kind"] for item in items] == ["agent", "user"]
    finally:
        context.close()
