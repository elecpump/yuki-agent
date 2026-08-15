from typing import Callable

DEFAULT_BASE_PROMPT = (
    "你是{persona},一个温柔的中文语音陪伴 agent。"
    "回复简短自然(1-3 句),贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
    "可以用工具查询记忆,但不要捏造记忆内容。"
)


def format_preferences(preferences: list[dict]) -> str:
    if not preferences:
        return ""
    lines = ["用户偏好："]
    for p in preferences:
        lines.append(f"- {p.get('content', '')}")
    return "\n".join(lines)


def format_soul_params(params: dict) -> str:
    if not params:
        return ""
    lines = ["参数说明："]
    for key, value in params.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def generate(persona_name: str, preferences: list[dict], soul_params: dict,
             base_prompt: str | None = None,
             refine: Callable[[str], str] | None = None) -> str:
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
