from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.loop import AgentLoop, make_summarize
from yuki.cognition.l2.view import CloudViewBuilder
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

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
        loop_kw: dict | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._max_turns = max_turns
        self._system = system_prompt or DEFAULT_PERSONA_PROMPT.format(persona=persona_name)
        summarize = make_summarize(client)
        self._view_builder = view_builder or CloudViewBuilder()
        self.loop = AgentLoop(
            client,
            registry,
            system_prompt=self._system,
            view_builder=self._view_builder,
            summarize=summarize,
            max_steps=max_turns,
            **(loop_kw or {}),
        )

    def set_system_prompt(self, text: str) -> None:
        self._system = text
        self.loop.set_system_prompt(text)

    def refine_persona(self, text: str) -> str:
        messages = [
            {"role": "system", "content": REFINE_PROMPT},
            {"role": "user", "content": text},
        ]
        response = self._client.chat(messages, timeout_s=5.0)
        return (response["choices"][0]["message"].get("content") or "").strip()

    def generate(
        self,
        utterance: str,
        context: ContextSnapshot | None = None,
        memory: MemoryManager | None = None,
    ) -> str:
        try:
            result = self.loop.run(utterance, context or ContextSnapshot(), memory)
            text = (result.get("text") or "").strip()
            if result.get("failed"):
                raise CloudError(f"tool loop exceeded max_turns={self._max_turns}")
            if result.get("interrupted"):
                raise CloudError("agent loop interrupted")
            if not text:
                raise CloudError("empty assistant reply")
            return text
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"generate failed: {exc}") from exc
