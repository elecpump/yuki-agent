from yuki.cognition.l2.context import build_cloud_context
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def test_context_includes_utterance_and_situation():
    ctx = build_cloud_context("你好", {"topic": "量子计算", "summary": "介绍", "key_points": ["a", "b"]})
    assert "你好" in ctx
    assert "量子计算" in ctx
    assert "a" in ctx


def test_context_filters_high_sensitivity_memory(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通记忆内容", sensitivity=0)
    manager.write("personal", "高敏记忆机密", sensitivity=2)
    ctx = build_cloud_context("记忆", memory=manager)
    assert "普通记忆内容" in ctx
    assert "高敏记忆机密" not in ctx


def test_context_never_raises_with_nothing():
    ctx = build_cloud_context("", situation=None, memory=None)
    assert "用户说" in ctx


def test_context_omits_empty_memory_section(tmp_path):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    ctx = build_cloud_context("无匹配内容xyz", memory=manager)
    assert "相关记忆" not in ctx
