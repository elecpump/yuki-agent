from dataclasses import dataclass, field
from typing import Protocol

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.l1 import L1Engine
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.actions")


@dataclass(frozen=True)
class Action:
    name: str
    params: dict = field(default_factory=dict)


@dataclass
class ActionContext:
    intent: Intent
    emotion: Emotion
    text: str = ""
    situation: dict | None = None
    memory: MemoryManager | None = None
    registry: FunctionRegistry | None = None
    l1: L1Engine | None = None


class ActionExecutor(Protocol):
    def __call__(self, action: Action, ctx: ActionContext) -> str: ...


DEFAULT_JOKES = ("为什么程序员分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。", "我不冷，我只是穿得少。")
DEFAULT_STORIES = ("从前有座山，山里有个庙，庙里有个老和尚在讲故事……")


def _empathize(action: Action, ctx: ActionContext) -> str:
    templates = {
        Emotion.SADNESS: "听起来你今天不太开心，我一直都在。",
        Emotion.ANXIETY: "别太紧张，我们慢慢来，我陪着你。",
        Emotion.ANGER: "听起来让你很生气，想说说怎么回事吗？",
        Emotion.LOVE: "我也想你呀，一直在呢。",
        Emotion.TIRED: "辛苦了，今天是不是很累？",
        Emotion.JOY: "太好啦，替你开心！",
        Emotion.NEUTRAL: "嗯，我在认真听。",
    }
    return templates.get(ctx.emotion, templates[Emotion.NEUTRAL])


def _acknowledge(action: Action, ctx: ActionContext) -> str:
    topic = action.params.get("topic") or (ctx.situation or {}).get("topic")
    if topic:
        return f"嗯，你正在看{topic}。"
    return "嗯嗯。"


def _comfort(action: Action, ctx: ActionContext) -> str:
    return "抱抱你，不管怎样都有我陪着你。"


def _encourage(action: Action, ctx: ActionContext) -> str:
    return "我相信你可以的，慢慢来。"


def _ask(action: Action, ctx: ActionContext) -> str:
    topic = (ctx.situation or {}).get("topic")
    if topic:
        return f"关于{topic}，你更想聊哪方面？"
    return "然后呢？"


def _clarify(action: Action, ctx: ActionContext) -> str:
    return "嗯？我可能没太听懂，你能再说一遍吗？"


def _inform(action: Action, ctx: ActionContext) -> str:
    if ctx.l1 is not None:
        return ctx.l1.reply(ctx.text or "", context=ctx.situation)
    return "嗯嗯，我在听。"


def _joke(action: Action, ctx: ActionContext) -> str:
    items = action.params.get("items")
    if items is None:
        items = DEFAULT_JOKES
    return items[0] if items else "这个我还在学，先陪你聊点别的吧。"


def _story(action: Action, ctx: ActionContext) -> str:
    items = action.params.get("items")
    if items is None:
        items = DEFAULT_STORIES
    return items[0] if items else "这个我还在学，先陪你聊点别的吧。"


def _invite_game(action: Action, ctx: ActionContext) -> str:
    return "要不要玩个成语接龙？我先来：一心一意。"


def _farewell(action: Action, ctx: ActionContext) -> str:
    return "好，再见啦，随时找我。"


def _safety_escalate(action: Action, ctx: ActionContext) -> str:
    return ("我在。你现在还好吗？如果很难受，请一定向身边信任的人求助，"
            "或者拨打心理援助热线。不要一个人扛着，我一直陪着你。")


def _write_memory(action: Action, ctx: ActionContext) -> str:
    if ctx.memory is not None:
        ctx.memory.write(
            action.params.get("memory_type", "scenario"),
            action.params.get("content", ctx.text or ""),
            source="brain",
            sensitivity=action.params.get("sensitivity", 0),
            metadata=action.params.get("metadata", {}),
        )
    return ""


def _call_function(action: Action, ctx: ActionContext) -> str:
    if ctx.registry is not None:
        name = action.params.get("name", "")
        args = action.params.get("arguments", {})
        try:
            ctx.registry.call(name, args)
        except Exception:
            logger.warning("function action failed", name=name, exc_info=True)
            pass
    return ""


def _stay_silent(action: Action, ctx: ActionContext) -> str:
    return ""


ACTION_EXECUTORS: dict[str, ActionExecutor] = {
    "empathize": _empathize,
    "acknowledge": _acknowledge,
    "comfort": _comfort,
    "encourage": _encourage,
    "ask": _ask,
    "clarify": _clarify,
    "inform": _inform,
    "joke": _joke,
    "story": _story,
    "invite_game": _invite_game,
    "farewell": _farewell,
    "safety_escalate": _safety_escalate,
    "write_memory": _write_memory,
    "call_function": _call_function,
    "stay_silent": _stay_silent,
}
