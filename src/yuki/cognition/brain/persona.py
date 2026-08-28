import re
from typing import Callable

DEFAULT_BASE_PROMPT = (
    "你是{persona},一个温柔的中文语音陪伴 agent。"
    "回复简短自然(1-3 句),贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
    "可以用工具查询记忆,但不要捏造记忆内容。"
)

TRAIT_DESCRIPTIONS = {
    "warmth": ("表达更克制", "表达温暖"),
    "humor": ("少用玩笑", "可以轻微幽默"),
    "directness": ("更委婉铺垫", "更直接清楚"),
    "proactiveness": ("减少主动插话", "更主动接话"),
    "empathy": ("情绪回应简洁", "更重视共情"),
}


def format_preferences(preferences: list[dict]) -> str:
    if not preferences:
        return ""
    lines = ["用户偏好："]
    for p in preferences:
        lines.append(f"- {p.get('content', '')}")
    return "\n".join(lines)


def format_core_values(core_values: list[dict]) -> str:
    values = [value for value in core_values if isinstance(value, dict) and value.get("text")]
    if not values:
        return ""
    lines = ["人格内核："]
    for value in values:
        role = value.get("role", "guiding")
        lines.append(f"- [{role}] {value.get('text', '')}")
    return "\n".join(lines)


def format_personality_traits(traits: dict) -> str:
    if not traits:
        return ""
    parts = []
    for name, (low, high) in TRAIT_DESCRIPTIONS.items():
        value = traits.get(name)
        if not isinstance(value, (int, float)):
            continue
        if value >= 0.58:
            parts.append(high)
        elif value <= 0.42:
            parts.append(low)
    if not parts:
        return "性格参数：保持均衡、自然、贴近当下语境。"
    return "性格参数：" + "；".join(parts) + "。"


def format_soul_params(params: dict) -> str:
    if not params:
        return ""
    lines = ["参数说明："]
    for key, value in params.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def format_soul(soul: dict | None) -> str:
    if not soul:
        return ""
    description = (soul.get("personality_description") or "").strip()
    sections = [part.strip() for part in re.split(r"\n\s*\n", description) if part.strip()]
    core = format_core_values(soul.get("core_values") or [])
    _replace_or_append_section(sections, "人格内核：", core)
    traits = format_personality_traits(soul.get("personality_traits") or {})
    _replace_or_append_section(sections, "性格参数：", traits)
    return "\n\n".join(sections)


def _replace_or_append_section(sections: list[str], heading: str, derived: str) -> None:
    if not derived:
        return
    matches = [
        index
        for index, section in enumerate(sections)
        if section.splitlines()[0].strip().startswith(heading)
    ]
    if not matches:
        sections.append(derived)
        return
    sections[matches[0]] = derived
    for index in reversed(matches[1:]):
        del sections[index]


def compose_personality_description(
    soul: dict,
    *,
    base_description: str,
    refine: Callable[[str], str] | None = None,
) -> str:
    draft = "\n\n".join(
        section
        for section in (
            base_description.strip(),
            format_core_values(soul.get("core_values") or []),
            format_personality_traits(soul.get("personality_traits") or {}),
        )
        if section
    )
    if refine is not None:
        try:
            refined = refine(draft)
            if refined and refined.strip():
                return refined.strip()
        except Exception:
            pass
    return draft


def generate(persona_name: str, preferences: list[dict], soul_params: dict,
             base_prompt: str | None = None,
             refine: Callable[[str], str] | None = None,
             soul: dict | None = None) -> str:
    base = format_soul(soul)
    if not base:
        base = (base_prompt or DEFAULT_BASE_PROMPT).format(persona=persona_name)
    prefs = format_preferences(preferences)
    params = format_soul_params(soul_params)
    text = base
    if prefs:
        text += "\n\n" + prefs
    if params:
        text += "\n\n" + params
    if refine is not None:
        try:
            refined = refine(text)
            if refined and refined.strip():
                return refined.strip()
        except Exception:
            pass  # 精修失败回退规则结果
    return text
