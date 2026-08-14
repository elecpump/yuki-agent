from yuki.memory.manager import MemoryManager

CLOUD_MEMORY_TOP_K = 3


def build_cloud_context(
    utterance: str,
    situation: dict | None = None,
    memory: MemoryManager | None = None,
) -> str:
    """组装发往云端的纯文本上下文。仅文本（不含帧/音频）；记忆过滤 sensitivity==2。"""
    parts = [f"用户说：{utterance or ''}"]
    if situation:
        topic = situation.get("topic", "") or ""
        summary = situation.get("summary", "") or ""
        points = situation.get("key_points") or []
        bits = [b for b in [topic, summary, *points] if b]
        if bits:
            parts.append("当前情境：" + " ".join(bits))
    if memory is not None:
        hits = memory.query(utterance or "", top_k=CLOUD_MEMORY_TOP_K, min_sensitivity=0)
        safe = [m for m in hits if m.get("sensitivity", 0) != 2]
        if safe:
            parts.append("相关记忆：\n" + "\n".join(f"- {m['content']}" for m in safe))
    return "\n".join(parts)
