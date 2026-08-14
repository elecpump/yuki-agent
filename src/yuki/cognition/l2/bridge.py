import json

from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.context import build_cloud_context
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

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
    ) -> None:
        self._client = client
        self._registry = registry
        self._max_turns = max_turns
        self._system = (system_prompt or DEFAULT_PERSONA_PROMPT).format(persona=persona_name)

    def generate(
        self,
        utterance: str,
        situation: dict | None = None,
        memory: MemoryManager | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": build_cloud_context(utterance, situation, memory)},
        ]
        tools = self._registry.tool_schemas() if self._registry else None
        try:
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
