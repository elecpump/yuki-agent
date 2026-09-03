"""Cloud agent loop orchestration."""

import json
import time
from collections.abc import Callable
from typing import Any

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.cognition.l2.view import CloudViewBuilder, estimate_tokens
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager

CRISIS_SYSTEM_PROMPT = (
    "你在和一位可能处于危机中的用户对话。只表达关怀、稳定情绪并建议求助，"
    "不要调用任何工具，不要追问细节。回复简短温暖。"
)

PREFERENCE_MEMORY_INSTRUCTION = (
    "仅当用户明确表达可长期复用且非敏感的偏好或纠正时，才可调用 memory.write，"
    '参数固定包含 memory_type="preference"、source="user"、sensitivity=0。'
    "涉及健康、身份、财务等敏感内容时，即使用户要求记住也不要写入；"
    "应简短说明当前不能持久保存敏感信息。"
    "不要从单次情绪、随口评价或模型推断中写入偏好；不确定时不要写。"
)

SUMMARIZE_PROMPT = (
    "请把以下内容压缩成 1-3 句简短中文摘要，保留关键事实与用户偏好，"
    "不要遗漏重要信息。"
)
SUMMARIZE_TIMEOUT_S = 2.0


def make_summarize(client: CloudClient) -> Callable[[list[str]], str]:
    def summarize(texts: list[str]) -> str:
        response = client.chat(
            [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": "\n".join(texts)},
            ],
            timeout_s=SUMMARIZE_TIMEOUT_S,
        )
        summary = (response["choices"][0]["message"].get("content") or "").strip()
        if not summary:
            raise CloudError("empty summary")
        return summary

    return summarize


class AgentLoop:
    """Run a cloud model until it produces a final reply."""

    def __init__(
        self,
        client: CloudClient,
        registry: FunctionRegistry | None = None,
        *,
        system_prompt: str,
        view_builder: CloudViewBuilder | None = None,
        summarize: Callable[[list[str]], str] | None = None,
        max_steps: int = 3,
        max_duration_s: float = 15.0,
        transition_fallback: str = "让我看一下……",
        tool_result_max_chars: int = 2000,
        compact_threshold_tokens: int = 0,
        clock: Callable[[], float] = time.monotonic,
        **_: Any,
    ) -> None:
        self._client = client
        self._registry = registry
        self._system = system_prompt
        self._summarize = summarize or make_summarize(client)
        self._view_builder = view_builder or CloudViewBuilder()
        self._max_steps = max_steps
        self._max_duration_s = max_duration_s
        self._transition_fallback = transition_fallback
        self._tool_result_max_chars = tool_result_max_chars
        self._compact_threshold_tokens = compact_threshold_tokens
        self._clock = clock

    def set_system_prompt(self, text: str) -> None:
        self._system = text

    def run(
        self,
        utterance: str,
        context: ContextSnapshot | None = None,
        memory: MemoryManager | None = None,
        *,
        crisis: bool = False,
        on_transition: Callable[[str], None] | None = None,
        interrupt_check: Callable[[], bool] | None = None,
        **_: Any,
    ) -> dict:
        started = self._clock()
        snapshot = context or ContextSnapshot()
        enriched = self._view_builder.enrich(snapshot, memory, utterance)
        view_text = self._view_builder.format(enriched, utterance)
        system_content = (
            CRISIS_SYSTEM_PROMPT
            if crisis
            else f"{self._system}\n\n{PREFERENCE_MEMORY_INSTRUCTION}"
        )
        messages = [{"role": "system", "content": system_content}]
        messages.append({"role": "user", "content": view_text})
        tools = (
            None
            if crisis
            else self._registry.tool_schemas(wire_names=True)
            if self._registry is not None
            else None
        )
        transition_sent = False

        def interrupted(steps: int) -> dict | None:
            if interrupt_check is not None and interrupt_check():
                return {"text": "", "steps": steps, "interrupted": True, "failed": False}
            return None

        def timed_out(steps: int) -> dict | None:
            if self._clock() - started >= self._max_duration_s:
                return {"text": "", "steps": steps, "interrupted": False, "failed": True}
            return None

        for step in range(1, self._max_steps + 1):
            if result := interrupted(step - 1):
                return result
            remaining = self._max_duration_s - (self._clock() - started)
            if remaining <= 0:
                return {"text": "", "steps": step - 1, "interrupted": False, "failed": True}
            compaction_attempted = self._maybe_compact(messages, budget_s=remaining)
            remaining = self._max_duration_s - (self._clock() - started)
            if remaining <= 0:
                return {"text": "", "steps": step - 1, "interrupted": False, "failed": True}
            if compaction_attempted and (result := interrupted(step - 1)):
                return result
            response = self._client.chat(messages, tools=tools, timeout_s=remaining)
            if result := interrupted(step - 1):
                return result
            if result := timed_out(step - 1):
                return result
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = (message.get("content") or "").strip()
                if not content:
                    raise CloudError("empty assistant reply")
                if result := interrupted(step - 1) or timed_out(step - 1):
                    return result
                return {
                    "text": content,
                    "steps": step,
                    "interrupted": False,
                    "failed": False,
                }
            if crisis:
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                })
                for call in tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps({
                            "ok": False,
                            "error": {
                                "code": "crisis_tool_calls_blocked",
                                "message": "tools are disabled in crisis mode",
                            },
                        }, ensure_ascii=False),
                    })
                continue
            if on_transition is not None and not transition_sent:
                if result := interrupted(step - 1):
                    return result
                transition = (message.get("content") or "").strip()
                on_transition(transition or self._transition_fallback)
                transition_sent = True
                if result := interrupted(step - 1):
                    return result
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                if result := interrupted(step - 1):
                    return result
                if result := timed_out(step - 1):
                    return result
                function = call.get("function") or {}
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else raw_arguments
                    )
                    if not isinstance(arguments, dict):
                        raise TypeError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {
                        "ok": False,
                        "error": {
                            "code": "invalid_tool_arguments",
                            "message": str(exc),
                        },
                    }
                else:
                    result = (
                        self._registry.dispatch({
                            "name": function.get("name", ""),
                            "arguments": arguments,
                        })
                        if self._registry is not None
                        else {"ok": False, "error": {"message": "no registry"}}
                    )
                if interrupted_result := interrupted(step - 1):
                    return interrupted_result
                if timeout_result := timed_out(step - 1):
                    return timeout_result
                serialized = json.dumps(result, ensure_ascii=False)
                if len(serialized) > self._tool_result_max_chars:
                    serialized = (
                        serialized[: self._tool_result_max_chars]
                        + "\n…（已截断）"
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": serialized,
                })
        return {"text": "", "steps": self._max_steps, "interrupted": False, "failed": True}

    def _maybe_compact(self, messages: list[dict], *, budget_s: float) -> bool:
        if self._compact_threshold_tokens <= 0:
            return False
        if budget_s <= SUMMARIZE_TIMEOUT_S:
            return False
        estimated_tokens = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        if estimated_tokens <= self._compact_threshold_tokens:
            return False
        system_count = 0
        while (
            system_count < len(messages)
            and messages[system_count].get("role") == "system"
        ):
            system_count += 1
        tool_assistants = [
            index for index, message in enumerate(messages) if message.get("tool_calls")
        ]
        if len(tool_assistants) <= 1:
            return False
        last_completed_pair = tool_assistants[-1]
        removed = messages[system_count:last_completed_pair]
        try:
            summary = self._summarize([
                json.dumps(message, ensure_ascii=False) for message in removed
            ])
        except Exception:
            return True
        if not summary:
            return True
        messages[:] = [
            *messages[:system_count],
            {"role": "user", "content": f"（已完成工具过程摘要）{summary}"},
            *messages[last_completed_pair:],
        ]
        return True
