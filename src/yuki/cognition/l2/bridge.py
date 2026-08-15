import json

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.view import SUMMARIZE_TIMEOUT_S, CloudViewBuilder
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

SUMMARIZE_PROMPT = (
    "请把以下对话压缩成 1-3 句简短中文摘要，"
    "保留关键事实与用户偏好，不要遗漏重要信息。"
)

REFINE_PROMPT = (
    "把以下人格描述润色得更自然、更有温度,保持语义不变。"
    "只输出润色后的文本:"
)

DEFAULT_PERSONA_PROMPT = (
    "你是{persona}，一个温柔的中文语音陪伴 agent。"
    "回复简短自然（1-3 句），贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时，优先表达关怀并建议求助。"
    "可以用工具查询记忆，但不要捏造记忆内容。"
)


class CloudBridge:
    """L2 云桥：请求构建 + 工具调用多轮 + 失败抛 CloudError。"""

    def __init__(
        self,
        client: CloudClient,
        registry: FunctionRegistry | None = None,
        system_prompt: str | None = None,
        max_turns: int = 3,
        persona_name: str = "yuki",
        view_builder: CloudViewBuilder | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._max_turns = max_turns
        self._system = system_prompt or DEFAULT_PERSONA_PROMPT.format(persona=persona_name)
        self._view_builder = view_builder or CloudViewBuilder(summarize=self._summarize_closure)

    def set_system_prompt(self, text: str) -> None:
        self._system = text

    def refine_persona(self, text: str) -> str:
        messages = [
            {"role": "system", "content": REFINE_PROMPT},
            {"role": "user", "content": text},
        ]
        response = self._client.chat(messages, timeout_s=5.0)
        return (response["choices"][0]["message"].get("content") or "").strip()

    def _summarize_closure(self, texts: list[str]) -> str:
        messages = [
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {"role": "user", "content": "\n".join(texts)},
        ]
        response = self._client.chat(messages, timeout_s=SUMMARIZE_TIMEOUT_S)
        return (response["choices"][0]["message"].get("content") or "").strip()

    def generate(
        self,
        utterance: str,
        context: ContextSnapshot | None = None,
        memory: MemoryManager | None = None,
    ) -> str:
        try:
            snapshot = self._view_builder.enrich(context, memory, utterance) \
                if context is not None else ContextSnapshot()
            view_text = self._view_builder.format(snapshot, utterance)
            messages = [
                {"role": "system", "content": self._system},
                {"role": "user", "content": view_text},
            ]
            tools = self._registry.tool_schemas() if self._registry else None
            for _ in range(self._max_turns):
                response = self._client.chat(messages, tools=tools)
                message = response["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = (message.get("content") or "").strip()
                    if not content:
                        raise CloudError("empty assistant reply")
                    return content
                messages.append({"role": "assistant", "content": message.get("content") or "",
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    fn = call.get("function") or {}
                    raw_args = fn.get("arguments", "{}")
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        arguments = {}
                    if self._registry is not None:
                        result = self._registry.dispatch({
                            "name": fn.get("name", ""), "arguments": arguments})
                    else:
                        result = {"ok": False, "error": {"message": "no registry"}}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            raise CloudError(f"tool loop exceeded max_turns={self._max_turns}")
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"generate failed: {exc}") from exc
