import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.view import (
    MAX_UTTERANCE_CHARS,
    SITUATION_TOKENS,
    SUMMARIZE_MAX_FAILURES,
    CloudViewBuilder,
    estimate_tokens,
)
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_snapshot(*, turns=(), situation=None):
    return ContextSnapshot(situation=situation, recent_turns=tuple(turns))


def turn(text, kind="user"):
    return {"content": text, "kind": kind, "ts": 0.0}


def test_estimate_tokens():
    assert estimate_tokens("你好") == 2   # ceil(2/1.5)=ceil(1.33)=2
    assert estimate_tokens("a" * 30) == 20


def test_enrich_short_conversation_no_summarize():
    calls = []
    builder = CloudViewBuilder(summarize=lambda texts: calls.append(texts) or "摘要")
    snap = make_snapshot(turns=[turn("t0"), turn("t1"), turn("t2"), turn("t3"), turn("t4")])
    out = builder.enrich(snap, None, "你好")
    assert out.summaries == ()          # 预算足够 → 不折叠
    assert calls == []


def test_enrich_long_conversation_folds_and_caches():
    calls = []

    def fake_summarize(texts):
        calls.append(texts)
        return "旧轮摘要"

    builder = CloudViewBuilder(summarize=fake_summarize, max_tokens=250)
    # 30 轮 → 超出逐字预算 → 折叠（base≈238 > 逐字预算，max_tokens=250 介于两者之间）
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(30)]
    snap = make_snapshot(turns=turns)
    out1 = builder.enrich(snap, None, "你好")
    assert calls  # 调了摘要
    assert any("摘要" in s for s in out1.summaries)
    # 缓存复用：再 enrich 不调摘要
    n_calls = len(calls)
    out2 = builder.enrich(snap, None, "你好")
    assert len(calls) == n_calls
    assert out2.summaries == out1.summaries


def test_enrich_summarize_failure_placeholder_and_circuit_breaker():
    def boom(texts):
        raise RuntimeError("summarize down")

    builder = CloudViewBuilder(summarize=boom, max_tokens=250)
    # 12 轮 → 折叠 8 轮 → 恰好 2 个折叠段（6+2），一次 enrich 触发 2 次失败（<3，不熔断）
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(12)]
    snap = make_snapshot(turns=turns)
    out = builder.enrich(snap, None, "x")
    assert builder._summarize_failures == 2
    assert builder._summarize_broken is False
    assert "之前聊了" in out.summaries[0]
    # 失败结果不缓存 → 第二次 enrich 再失败 2 次，累计 4 >= 3 → 熔断
    builder.enrich(snap, None, "x")
    assert builder._summarize_failures >= SUMMARIZE_MAX_FAILURES
    assert builder._summarize_broken is True


def test_enrich_summarize_none_placeholder():
    builder = CloudViewBuilder(summarize=None, max_tokens=250)
    turns = [turn(f"第{i}轮内容内容内容内容内容") for i in range(30)]
    snap = make_snapshot(turns=turns)
    out = builder.enrich(snap, None, "x")
    assert out.summaries and "之前聊了" in out.summaries[0]


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
    assert text.startswith("用户说：")
    assert "量子计算" in text
    assert "逐字轮1" in text
    assert "你好呀" in text  # 截断但保留开头


def test_format_empty_snapshot():
    builder = CloudViewBuilder()
    text = builder.format(make_snapshot(), "")
    assert "用户说：" in text
