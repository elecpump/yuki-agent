import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import (
    CloudViewBuilder,
    estimate_tokens,
)
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_snapshot(*, turns=(), situation=None):
    return ContextSnapshot(situation=situation, recent_turns=tuple(turns))


def test_enrich_preserves_persistent_fallback_turns():
    fallback = (turn("持久化历史"),)
    snapshot = ContextSnapshot(fallback_turns=fallback)

    enriched = CloudViewBuilder().enrich(snapshot, None, "当前问题")

    assert enriched.fallback_turns == fallback


def test_enrich_does_not_overwrite_persistent_segment_summaries():
    snapshot = ContextSnapshot(summaries=("持久化摘要",))

    enriched = CloudViewBuilder().enrich(snapshot, None, "当前问题")

    assert enriched.summaries == ("持久化摘要",)


def turn(text, kind="user"):
    return {"content": text, "kind": kind, "ts": 0.0}


def test_estimate_tokens():
    assert estimate_tokens("你好") == 2   # ceil(2/1.5)=ceil(1.33)=2
    assert estimate_tokens("a" * 30) == 20


def test_enrich_memory_filters_private_and_high_sensitivity(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "喜欢安静", sensitivity=0)
    manager.write("preference", "私密安静偏好", sensitivity=1)
    manager.write("personal", "安静机密", sensitivity=2)
    builder = CloudViewBuilder()
    snap = make_snapshot()
    out = builder.enrich(snap, manager, "安静")
    contents = [m["content"] for m in out.long_term_memory]
    assert "喜欢安静" in contents
    assert "私密安静偏好" not in contents
    assert "安静机密" not in contents


def test_format_order_and_quota():
    builder = CloudViewBuilder()
    snap = make_snapshot(
        situation={"topic": "量子计算", "summary": "介绍", "key_points": ["a"]},
        turns=[turn("逐字轮1", "user"), turn("逐字轮2", "agent")],
    )
    text = builder.format(snap, "你好呀" * 200)  # 超长 utterance → 截断
    assert text.splitlines()[-1].startswith("用户说：")
    assert "量子计算" in text
    assert "逐字轮1" in text
    assert "你好呀" in text  # 截断但保留开头


def test_format_empty_snapshot():
    builder = CloudViewBuilder()
    text = builder.format(make_snapshot(), "")
    assert "用户说：" in text


def test_format_includes_pending_summary_fallback_turns():
    snapshot = ContextSnapshot(fallback_turns=(turn("上一段原文"),))

    text = CloudViewBuilder().format(snapshot, "当前问题")

    assert "上一段原文" in text


def test_format_uses_remaining_budget_for_active_segment_candidates():
    snapshot = make_snapshot(turns=[turn(f"活跃轮次{i}") for i in range(6)])

    text = CloudViewBuilder(max_tokens=1000, verbatim_turns=4).format(snapshot, "继续")

    assert "活跃轮次5" in text
