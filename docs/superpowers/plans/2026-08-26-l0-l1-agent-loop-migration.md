# L0+L1 Agent Loop 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有"本地分类 → 固定路由"（O(1) 反应式管道）迁移为 L0 守门员（1.7B 二分类 local/cloud）+ L1 云端 Agent Loop（多轮工具调用、过渡文本、用户插话中断、预算控制）。同时移除 Sedimenter 与 Intent 枚举、简化 Tuner 为仅 cooldown，并修复设计评审发现的三个阻断问题：插话标记与 `_decision_lock` 冲突、过渡文本与 TTS latest-reply-wins 冲突、Sedimenter 移除后 persona 刷新链断裂。

**Architecture:** 新增 `AgentLoop`（`cognition/l2/loop.py`）承载多步工具循环，取代 `CloudBridge.generate` 的循环体；`CloudBridge` 保留为摘要、精修与兼容入口。`DecisionHub` 的 cloud 路径改为调用 `AgentLoop.run`。

transition、final 与 cancel 复用 `REPLY` topic，并以 `reply_id` 关联同一轮。Interaction/TTS 按 kind 调度，Gateway 历史只记录 final。用户插话由不取 `_decision_lock` 的第二个 Bus 订阅者探针记录，loop 在阻塞边界前后检查。

`LocalRouter` 从 4 路由改为 `GateRoute.LOCAL/CLOUD` 二分类；persona 按 utterance 数量周期刷新。L0 将显式偏好/纠正送 cloud，但只有非敏感偏好可由 `memory.write` 以 sensitivity=0 落库；Tuner 只调 cooldown 并自举 floor。

**Tech Stack:** Python ≥3.11，stdlib + 现有依赖。无新增运行时依赖。

## Global Constraints

- **REPLY 载荷**：新增可选 `kind`（`"final"` 默认 / `"transition"` / `"cancel"`）与 `reply_id`；旧发布者缺省按 final 处理。transition/final/cancel 必须使用同一 `reply_id`；`Topics` 不变。
- **危机路径**：规则层（`CRISIS_KEYWORDS`）拦截 → 无工具 AgentLoop（`crisis=True`，专用 system prompt）→ 失败/中断/空回复一律回退 `CRISIS_FALLBACK_REPLY`。`tools=None` 只是协议层第一道防线；若模型仍返回 tool_calls，AgentLoop 必须写回 `crisis_tool_calls_blocked` tool error，绝不 dispatch、绝不发布 transition。
- **插话中断**：探针不取 `_decision_lock`，只做 `max(ts)` 记录；缺失/非法 `ts` 记录 warning 并忽略，禁止用消费时刻伪造。loop 在模型调用前后、发布 transition 前、每个工具调用前后、返回 final 前检查。中断后不发 final/不可用提示；若已发 transition，则发布同 `reply_id` 的 cancel 让 TTS 停止。危机路径是显式例外：中断仍发布危机兜底，spec 与测试必须写清。
- **过渡文本**：每个 loop 至多 1 条（配置改为布尔 `transition_enabled`，不暴露无效的 >1 配额）；模型 content 为空但带 tool_calls 时用兜底文案（`transition_fallback`，默认"让我看一下……"）；`transition_enabled=false` 时完全不发。final 不得被 transition 打断；final 到达时立即使尚未出声的 pending/合成中 transition 失效，只有已 `_mark_speaking` 的 transition 才可自然收尾最多 `transition_grace_s`（默认 0.8s），随后停止并播放 final。
- **工具结果截断**：AgentLoop 序列化工具结果时截断（`tool_result_max_chars`，默认 2000），防止 `text.extract`（50k 字符上限）等大 payload 撑爆上下文。
- **工具参数错误**：tool call 的 `arguments` 非法 JSON 或非 object 时不得 dispatch；向模型追加结构化 tool error 后继续循环。
- **预算压缩**：`compact_threshold_tokens=0`（默认）关闭；开启时只折叠**已完成的** assistant(tool_calls)+tool 消息对（成对丢弃，保持 OpenAI 消息合法性），始终保留全部 system 消息和最近一组已完成工具调用，折叠产物为 LLM 摘要（`SUMMARIZE_TIMEOUT_S=2.0`）。
- **wall-clock 预算**：每次模型调用传入剩余 `timeout_s`，调用返回后再次检查 deadline；超过预算的文本/工具结果一律丢弃。同步工具无法被 Python 安全强杀，工具自身必须有超时；loop 在工具前后检查并保证超时后不再调用后续工具或发布回复。该约束保证“不会发布过期结果”，不承诺强制终止已经开始的有副作用工具。
- **偏好来源**：删除 Sedimenter 的隐式多信号学习；保留显式、非敏感偏好能力。L0 prompt 将“我喜欢/不喜欢/以后请/不要/说反了”等持久偏好与纠正路由为 cloud，L1 仅在用户明确表达长期且非敏感偏好时调用 `memory.write(memory_type="preference", source="user", sensitivity=0)`。云端 memory.write schema 只接受 sensitivity=0；健康、身份、财务等敏感内容即使用户要求记住也不得调用该工具，需说明当前不能持久保存。禁止从单次隐式情绪推断偏好。
- **请求域中断**：Bus 语音回合（`publish_reply=True`）接入 utterance probe；Gateway `cognition.chat`（`publish_reply=False`）不传 `interrupt_check`，不会被桌面语音插话静默打断。
- **命名与情绪**：文档层 L0/L1；代码层 `AgentLoop` 即"L1 循环"，`cognition/l2/` 包名保留（改名成本高，不做）。`Intent` 枚举删除；`Emotion` 保留（REPLY 的 emotion 字段仍被 TTS `EmotionMapper` 消费）。local final 与成功的 cloud final 使用 `detect_emotion(text)`；`L2_UNAVAILABLE_NOTICE`、transition、cancel 固定 neutral，危机固定 sadness。
- **配置兼容**：`cloud.max_turns` 保留为弃用字段；`agent_loop.max_steps` 设置后优先。删除顶层 `sedimenter:` 配置节会使旧配置在 `Config.extra="forbid"` 下报错，迁移时必须删；删除嵌套 `local_brain.local_tool_allowlist` 后旧键按当前 Pydantic 嵌套模型设置会被忽略，但也应从存量配置移除。
- 每个任务都必须保持可导入、可测试、可 commit；由于 Task 3 删除旧偏好链而 Task 4 才完成新显式偏好路由，只有 Task 5 完成后的整条迁移可部署，禁止从中间 commit 发布。测试命令：`& ".venv\Scripts\python.exe" -m pytest <file> -v`；全仓 `-m pytest`（e2e 默认跳过）；收尾跑 `-m pytest -m e2e`。

## 评审问题 → 任务映射

| 评审问题 | 处理 |
| --- | --- |
| 插话标记被 `_decision_lock` 阻塞 | Task 2：无锁探针订阅者 + `ts > loop_start` 判定 |
| 过渡文本 vs TTS latest-reply-wins | Task 2：reply_id/cancel；Task 5：kind-aware TTS + Gateway 仅记录 final |
| Sedimenter 移除后 persona 刷新链断裂 | Task 3：按 utterance 数量触发 persona_refresh；Task 1/4：显式偏好路由到 cloud，仅非敏感内容由 memory.write 落库 |
| REPLY emotion 消费者被连带破坏 | Task 4：保留 Emotion 枚举 + `detect_emotion` 关键词检测 |
| CloudBridge 循环重复实现风险 | Task 1：AgentLoop 取代 generate 循环体，generate 变兼容薄壳 |
| VISION/TOOL_LOCAL 快速通道删除 | Task 4：2 分类后视觉/工具问题统一走 L1 工具（vision.understand/text.extract）；本地 VLM 仍供情境深度理解；`screen.py` 保留备用 |
| 工具 payload 撑爆上下文 | Task 1：工具结果截断 + 可选成对压缩 |
| 危机路径工具暴露与兜底 | Task 1：`tools=None` + `crisis_tool_calls_blocked` 硬守卫；Task 2：`CRISIS_FALLBACK_REPLY` |
| 敏感偏好写后不可读 | Task 0/1/4：选择保守策略，仅持久化 sensitivity=0 的非敏感显式偏好；敏感内容不落库并向用户说明 |
| Gateway 聊天被语音插话打断 | Task 2：仅 `publish_reply=True` 的语音回合传入 `interrupt_check` |
| final 到达时 transition 尚在合成 | Task 5：pending/合成中 transition 立即失效；仅已播放 transition 适用 grace |
| 云端失败通知继承用户愤怒情绪 | Task 4：成功回复检测情绪，`L2_UNAVAILABLE_NOTICE` 固定 neutral |
| 无 wall-clock 预算 | Task 1：剩余 timeout 传给 client + 每个阻塞边界返回后复检 |
| Tuner floor 机制随 Sedimenter 丢失 | Task 3：Tuner 负面反馈自举 floor（`floor_negatives` 次后 +30s） |

---

## 文件结构

**新增**
- `src/yuki/cognition/l2/loop.py` — `AgentLoop` + `CRISIS_SYSTEM_PROMPT` + `make_summarize`
- `tests/cognition/l2/test_loop.py` — AgentLoop 单测
- `tests/cognition/test_classifier.py` — Emotion/`detect_emotion` 单测
- `docs/superpowers/specs/2026-08-26-l0-l1-agent-loop-design.md` — 设计 spec 落库（Task 0）

**修改**
- `src/yuki/cognition/l2/bridge.py` — `CloudBridge` 持有/暴露 `AgentLoop`，`generate` 变薄壳
- `src/yuki/cognition/l2/__init__.py` — 导出 `AgentLoop`
- `src/yuki/cognition/brain/hub.py` — cloud 路径换 loop、插话探针、REPLY kind、crisis 无工具、路由简化、sinks/periodic、trace 去 intent
- `src/yuki/cognition/brain/classifier.py` — 删 `Intent`，留 `Emotion` + `detect_emotion`
- `src/yuki/cognition/brain/local/router.py` — 2 分类 `GateRoute` 重写
- `src/yuki/cognition/brain/tuner.py` — 去 traits，cooldown + floor 自举
- `src/yuki/cognition/brain/sink.py` — 删 `SedimenterSink`/`on_engagement`，留 `TunerSink`
- `src/yuki/cognition/brain/soul.py` — 删 `adjust_traits`/`on_preference_sedimented`/`apply_core_value_feedback` 等死代码 + `FLOOR_KEY`
- `src/yuki/cognition/brain/__init__.py` — 导出更新
- `src/yuki/cognition/assembly.py` — 装配 AgentLoop、periodic persona_refresh、去 sedimenter/vision_screen
- `src/yuki/functions/memory_tools.py` — cloud 工具写入只接受 sensitivity=0，拒绝不可回读的敏感写入
- `src/yuki/config.py` — `AgentLoopConfig` 新增、`PersonaConfig.refresh_every_utterances`、删 `SedimenterConfig`、`LocalBrainConfig` 去 allowlist、`CloudConfig.max_turns` 弃用
- `config.example.yaml` — 同步
- `src/yuki/interaction/agent.py` — REPLY `kind/reply_id` 分派，cancel 转 TTS 取消
- `src/yuki/interaction/tts_controller.py` — kind-aware transition/final 排队与 reply_id cancel
- `src/yuki/payloads.py` — `ReplyPayload.kind/reply_id`
- `src/yuki/bus_server/gateway.py` — history 仅把 final 作为正式 assistant turn
- 测试：`test_hub.py`、`test_cognition.py`、`test_config.py`、`test_sink.py`、`test_tuner.py`、`test_soul.py`、`test_assembly.py`、`test_local_router.py`、`tests/cognition/l2/test_bridge.py`、`tests/functions/test_memory_tools.py`、`tests/interaction/test_tts_controller.py`、`tests/interaction/test_interaction.py`、`tests/bus_server/test_gateway.py`

**删除**
- `src/yuki/cognition/brain/sedimenter.py`、`tests/cognition/test_sedimenter.py`
- `src/yuki/cognition/brain/route.py`、`tests/cognition/test_route.py`

---

### Task 0: 设计 spec 落库

**Files:**
- Create: `docs/superpowers/specs/2026-08-26-l0-l1-agent-loop-design.md`

**Interfaces:** 无代码接口。

- [ ] **Step 1: 创建 spec 文档**

把用户提供的"L0+L1 Agent Loop 认知架构设计（简略版）"落为 spec，并入评审修正后的决策。插话使用无锁探针 + `ts > loop_start`，每个阻塞边界前后复检；仅 `publish_reply=True` 的语音回合可被该探针中断。

过渡文本每 loop 限 1 条，以 `reply_id` 关联 transition/final/cancel。TTS 必须区分尚未出声与已播放的 transition：前者在 final 到达时立即失效，后者最多等待 grace；Gateway 历史只记录 final。

Sedimenter 移除后，persona 按 utterance 数量刷新。L0 将显式偏好请求送 cloud；只有非敏感偏好可用 `sensitivity=0` 调用 `memory.write`。健康、身份、财务等敏感内容不持久化，并明确告知用户。

Emotion 保留：local final 和成功的 cloud final 使用 `detect_emotion`，云端不可用通知固定 neutral。非法工具参数不 dispatch；危机模式除 `tools=None` 外还必须硬拦截 tool_calls，不得 dispatch。工具结果截断，模型调用传入剩余 deadline。结构沿用仓库 spec 模板（Goal/Architecture/数据流/接口/风险）。

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-08-26-l0-l1-agent-loop-design.md
git commit -m "docs: add L0+L1 agent loop design spec"
```

---

### Task 1: AgentLoop 核心（`cognition/l2/loop.py`）

**Files:**
- Create: `src/yuki/cognition/l2/loop.py`
- Create: `tests/cognition/l2/test_loop.py`
- Modify: `src/yuki/cognition/l2/__init__.py`
- Modify: `src/yuki/functions/memory_tools.py`
- Modify: `tests/functions/test_memory_tools.py`

**Interfaces:**
- Consumes: `CloudClient`、`FunctionRegistry`、`ContextSnapshot`、`CloudViewBuilder`、`MemoryManager`。
- Produces: `AgentLoop(client, registry=None, *, system_prompt, view_builder=None, summarize=None, max_steps=3, max_duration_s=15.0, tool_result_max_chars=2000, compact_threshold_tokens=0, transition_fallback="让我看一下……", clock=time.monotonic)`，方法 `set_system_prompt(text)`、`run(utterance, context=None, memory=None, *, crisis=False, on_transition=None, interrupt_check=None) -> {"text", "steps", "interrupted", "failed"}`。模型调用使用剩余 deadline 作为 `timeout_s`，并在每个阻塞边界前后复检；`crisis=True` 时硬拦截模型返回的所有 tool_calls。模块级提供 `CRISIS_SYSTEM_PROMPT`、`PREFERENCE_MEMORY_INSTRUCTION`、`make_summarize(client)`。Task 2 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/l2/test_loop.py`**

```python
import pytest

from yuki.cognition.l2.client import CloudError
from yuki.cognition.l2.loop import AgentLoop, CRISIS_SYSTEM_PROMPT
from yuki.functions.registry import FunctionRegistry


class TurnClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, timeout_s=None):
        self.calls.append((messages, tools, timeout_s))
        return self._responses.pop(0)


def _msg(content, tool_calls=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _tool_call(cid="c1", name="echo", arguments="{}"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _loop(client, registry=None, **kw):
    return AgentLoop(client, registry, system_prompt="你是测试助手", **kw)


def test_run_single_final_reply():
    client = TurnClient([_msg("你好呀")])
    result = _loop(client, FunctionRegistry()).run("你好")
    assert result == {"text": "你好呀", "steps": 1, "interrupted": False, "failed": False}
    assert client.calls[0][0][0]["role"] == "system"
    assert any("memory.write" in m.get("content", "") for m in client.calls[0][0])
    assert any("敏感" in m.get("content", "") and "不要写入" in m.get("content", "")
               for m in client.calls[0][0])
    assert client.calls[0][1] == []  # 空 registry → tools 空列表


def test_run_tool_loop_then_final():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("", [_tool_call()]), _msg("最终回答")])
    result = _loop(client, registry).run("测试")
    assert result["text"] == "最终回答"
    assert result["steps"] == 2
    second = client.calls[1][0]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "c1"
    assert second[-1]["role"] == "tool" and "ok" in second[-1]["content"]


def test_run_crisis_disables_tools_and_uses_crisis_prompt():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("我在。")])
    result = _loop(client, registry).run("我不想活了", crisis=True)
    assert result["text"] == "我在。"
    assert client.calls[0][0][0]["content"] == CRISIS_SYSTEM_PROMPT
    assert not any("memory.write" in m.get("content", "") for m in client.calls[0][0])
    assert client.calls[0][1] is None  # crisis 不带 tools


def test_run_crisis_blocks_hallucinated_tool_calls_without_dispatch():
    dispatched = []
    transitions = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: dispatched.append(1) or "ok")
    client = TurnClient([
        _msg("我查一下", [_tool_call()]),
        _msg("请立刻联系身边可信任的人。"),
    ])
    result = _loop(client, registry).run(
        "我不想活了", crisis=True, on_transition=transitions.append,
    )
    assert result["text"] == "请立刻联系身边可信任的人。"
    assert dispatched == []
    assert transitions == []
    tool_message = client.calls[1][0][-1]
    assert "crisis_tool_calls_blocked" in tool_message["content"]


def test_run_transition_callback_with_content():
    transitions = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("让我查一下", [_tool_call()]), _msg("结果")])
    _loop(client, registry).run("x", on_transition=transitions.append)
    assert transitions == ["让我查一下"]


def test_run_transition_fallback_when_content_empty():
    transitions = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("", [_tool_call()]), _msg("结果")])
    _loop(client, registry, transition_fallback="稍等").run("x", on_transition=transitions.append)
    assert transitions == ["稍等"]


def test_run_transition_only_once():
    transitions = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    responses = [_msg("第一次", [_tool_call()]), _msg("第二次", [_tool_call()]), _msg("完成")]
    client = TurnClient(responses)
    _loop(client, registry).run("x", on_transition=transitions.append)
    assert transitions == ["第一次"]


def test_run_interrupt_before_first_step():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("", [_tool_call()]), _msg("不该到达")])
    result = _loop(client, registry).run(
        "x", interrupt_check=lambda: True,
    )
    assert result["interrupted"] is True
    assert result["text"] == ""
    assert len(client.calls) == 0  # 第一个 step 前即中断


def test_run_interrupt_during_single_final_call_discards_reply():
    interrupted = [False]

    class InterruptingClient(TurnClient):
        def chat(self, messages, tools=None, timeout_s=None):
            response = super().chat(messages, tools=tools, timeout_s=timeout_s)
            interrupted[0] = True
            return response

    client = InterruptingClient([_msg("过期回答")])
    result = _loop(client).run("x", interrupt_check=lambda: interrupted[0])
    assert result == {"text": "", "steps": 0, "interrupted": True, "failed": False}
    assert len(client.calls) == 1


def test_run_interrupt_after_tool_plan_does_not_transition_or_dispatch():
    interrupted = [False]
    transitions = []
    dispatched = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: dispatched.append(1) or "ok")

    class InterruptingClient(TurnClient):
        def chat(self, messages, tools=None, timeout_s=None):
            response = super().chat(messages, tools=tools, timeout_s=timeout_s)
            interrupted[0] = True
            return response

    client = InterruptingClient([_msg("让我查", [_tool_call()])])
    result = _loop(client, registry).run(
        "x", on_transition=transitions.append, interrupt_check=lambda: interrupted[0],
    )
    assert result["interrupted"] is True
    assert transitions == []
    assert dispatched == []


def test_run_max_duration_fails():
    ticks = iter([0.0, 0.0, 0.0, 2.0])  # started / step 边界 / 压缩后 / 调用后
    client = TurnClient([_msg("你好呀")])
    loop = _loop(client, max_duration_s=1.0, clock=lambda: next(ticks))
    result = loop.run("你好")
    assert result["failed"] is True
    assert result["text"] == ""
    assert len(client.calls) == 1
    assert client.calls[0][2] == pytest.approx(1.0)


def test_run_exhaustion_fails_without_raise():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("", [_tool_call()])] * 3)
    result = _loop(client, registry, max_steps=3).run("x")
    assert result["failed"] is True
    assert len(client.calls) == 3


def test_run_empty_reply_raises_cloud_error():
    client = TurnClient([_msg("   ")])
    with pytest.raises(CloudError):
        _loop(client).run("x")


def test_run_tool_result_truncated():
    registry = FunctionRegistry()
    registry.tool("big", description="大", params=None)(lambda p: "长" * 5000)
    client = TurnClient([_msg("", [_tool_call(name="big")]), _msg("完成")])
    _loop(client, registry, tool_result_max_chars=100).run("x")
    tool_content = client.calls[1][0][-1]["content"]
    assert len(tool_content) <= 100 + 20  # 截断 + 标记
    assert "已截断" in tool_content


def test_run_compact_folds_completed_pairs():
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: "ok")
    client = TurnClient([_msg("", [_tool_call()]), _msg("", [_tool_call()]), _msg("完成")])
    loop = _loop(client, registry, compact_threshold_tokens=50)
    loop._summarize = lambda texts: "摘要内容"
    result = loop.run("x")
    assert result["text"] == "完成"
    last_messages = client.calls[2][0]
    summary_index = next(
        i for i, message in enumerate(last_messages) if "摘要内容" in message.get("content", "")
    )
    assert last_messages[summary_index]["role"] == "user"
    assert last_messages[summary_index + 1]["role"] == "assistant"  # 保留最后完成对


def test_run_bad_arguments_returns_tool_error_without_dispatch():
    dispatched = []
    registry = FunctionRegistry()
    registry.tool("echo", description="回显", params=None)(lambda p: dispatched.append(1) or "ok")
    client = TurnClient([_msg("", [_tool_call(arguments="{oops")]), _msg("完成")])
    result = _loop(client, registry).run("x")
    assert result["text"] == "完成"
    assert dispatched == []
    tool_message = client.calls[1][0][-1]
    assert tool_message["role"] == "tool"
    assert "invalid_tool_arguments" in tool_message["content"]
```

同时在 `tests/functions/test_memory_tools.py` 添加 `test_write_rejects_non_public_sensitivity`：分别提交 `sensitivity=1` 和 `2`，断言在参数校验阶段抛出 `ArgumentValidationError`，且 `MemoryManager` 中没有新增记录。再断言导出的 `memory.write` schema 将 sensitivity 限定为 0。

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_loop.py tests/functions/test_memory_tools.py -v`
Expected: FAIL（loop 测试因模块尚不存在失败；memory_tools 新测试因 sensitivity 1/2 尚可写入而失败）。

- [ ] **Step 3: 创建 `src/yuki/cognition/l2/loop.py`**

```python
"""L1 Agent Loop：云端模型在循环中自主决策调用工具或直接回复。

在 CloudBridge.generate 的单次循环之上增加：过渡文本回调、step 间用户插话
检查、wall-clock 预算、工具结果截断、可选已完成工具对压缩。"""

import json
import time

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

DEFAULT_TRANSITION_FALLBACK = "让我看一下……"

SUMMARIZE_PROMPT = (
    "请把以下内容压缩成 1-3 句简短中文摘要，保留关键事实与用户偏好，"
    "不要遗漏重要信息。"
)
SUMMARIZE_TIMEOUT_S = 2.0


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…(已截断)"


def make_summarize(client: CloudClient):
    """用同一云端 client 做 LLM 摘要压缩的闭包。"""

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
            raise CloudError("empty compaction summary")
        return summary

    return summarize


class AgentLoop:
    """多步工具循环。run() 返回结果 dict；协议/网络错误抛 CloudError。"""

    def __init__(
        self,
        client: CloudClient,
        registry: FunctionRegistry | None = None,
        *,
        system_prompt: str,
        view_builder: CloudViewBuilder | None = None,
        summarize=None,
        max_steps: int = 3,
        max_duration_s: float = 15.0,
        tool_result_max_chars: int = 2000,
        compact_threshold_tokens: int = 0,
        transition_fallback: str = DEFAULT_TRANSITION_FALLBACK,
        clock=time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._system = system_prompt
        self._summarize = summarize or make_summarize(client)
        self._view_builder = view_builder or CloudViewBuilder(summarize=self._summarize)
        self._max_steps = max_steps
        self._max_duration_s = max_duration_s
        self._tool_result_max_chars = tool_result_max_chars
        self._compact_threshold_tokens = compact_threshold_tokens
        self._transition_fallback = transition_fallback
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
        on_transition=None,
        interrupt_check=None,
    ) -> dict:
        """返回 {"text", "steps", "interrupted", "failed"}。

        - interrupted：任一阻塞边界命中插话，放弃本次最终回复；已发 transition 由 Hub cancel。
        - failed：超时/超步数，调用方走兜底（云端不可用提示或危机回复）。
        - 模型空回复、网络错误等协议问题抛 CloudError。
        """
        started = self._clock()

        def boundary(step: int) -> tuple[dict | None, float]:
            if interrupt_check is not None and interrupt_check():
                return (
                    {"text": "", "steps": step, "interrupted": True, "failed": False},
                    0.0,
                )
            remaining = self._max_duration_s - (self._clock() - started)
            if remaining <= 0:
                return (
                    {"text": "", "steps": step, "interrupted": False, "failed": True},
                    0.0,
                )
            return None, remaining

        try:
            snapshot = (
                self._view_builder.enrich(context, memory, utterance)
                if context is not None
                else ContextSnapshot()
            )
            view_text = self._view_builder.format(snapshot, utterance)
            messages = [{
                "role": "system",
                "content": CRISIS_SYSTEM_PROMPT if crisis else self._system,
            }]
            if not crisis:
                messages.append({"role": "system", "content": PREFERENCE_MEMORY_INSTRUCTION})
            messages.append({"role": "user", "content": view_text})
            tools = (
                None
                if crisis
                else (self._registry.tool_schemas() if self._registry is not None else None)
            )
            transition_used = False
            for step in range(self._max_steps):
                stopped, remaining = boundary(step)
                if stopped is not None:
                    return stopped
                # 摘要调用固定最多 2s；剩余预算不足时跳过压缩，不能挤占主回答 deadline。
                if remaining > SUMMARIZE_TIMEOUT_S:
                    self._maybe_compact(messages)
                stopped, remaining = boundary(step)
                if stopped is not None:
                    return stopped
                response = self._client.chat(messages, tools=tools, timeout_s=remaining)
                stopped, _ = boundary(step)
                if stopped is not None:
                    return stopped
                message = response["choices"][0]["message"]
                content = (message.get("content") or "").strip()
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    if not content:
                        raise CloudError("empty assistant reply")
                    stopped, _ = boundary(step)
                    if stopped is not None:
                        return stopped
                    return {
                        "text": content,
                        "steps": step + 1,
                        "interrupted": False,
                        "failed": False,
                    }
                if crisis:
                    # 防御兼容 API/模型在 tools=None 时仍幻觉 tool_calls：硬阻断执行。
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
                # 过渡文本：只在首个工具步发布一次（content 为空时用兜底文案）
                if not transition_used:
                    stopped, _ = boundary(step)
                    if stopped is not None:
                        return stopped
                    transition = content or self._transition_fallback
                    transition_used = True
                    if transition and on_transition is not None:
                        on_transition(transition)
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                })
                for call in tool_calls:
                    stopped, _ = boundary(step)
                    if stopped is not None:
                        return stopped
                    fn = call.get("function") or {}
                    raw_args = fn.get("arguments", "{}")
                    argument_error = False
                    try:
                        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, TypeError):
                        arguments = None
                        argument_error = True
                    if not isinstance(arguments, dict):
                        argument_error = True
                    if argument_error:
                        result = {
                            "ok": False,
                            "error": {
                                "code": "invalid_tool_arguments",
                                "message": "tool arguments must be a valid JSON object",
                            },
                        }
                    elif self._registry is not None:
                        result = self._registry.dispatch({
                            "name": fn.get("name", ""),
                            "arguments": arguments,
                        })
                    else:
                        result = {"ok": False, "error": {"message": "no registry"}}
                    stopped, _ = boundary(step)
                    if stopped is not None:
                        return stopped
                    payload = _truncate_text(
                        json.dumps(result, ensure_ascii=False),
                        self._tool_result_max_chars,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": payload,
                    })
            return {"text": "", "steps": self._max_steps, "interrupted": False, "failed": True}
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"agent loop failed: {exc}") from exc

    def _maybe_compact(self, messages: list[dict]) -> None:
        """预算触发：折叠较早的已完成工具对，保留 system 与最近一组工具调用。"""
        if self._compact_threshold_tokens <= 0:
            return
        total = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        if total <= self._compact_threshold_tokens:
            return
        system_count = 0
        while system_count < len(messages) and messages[system_count].get("role") == "system":
            system_count += 1
        tool_assistants = [i for i, msg in enumerate(messages) if msg.get("tool_calls")]
        if len(tool_assistants) <= 1:  # 仅一轮已完成工具调用，无可压缩
            return
        last = tool_assistants[-1]
        removed = messages[system_count:last]
        try:
            summary = self._summarize(
                [json.dumps(m, ensure_ascii=False) for m in removed]
            )
        except Exception:
            return
        messages[:] = [
            *messages[:system_count],
            {"role": "user", "content": f"（已完成工具过程摘要）{summary}"},
            *messages[last:],
        ]
```

同一任务内收紧云端记忆工具：把 `WriteParams.sensitivity` 改为 `Literal[0] = 0`，并把 `memory.write` 描述明确为“仅写入非敏感公开记忆”。这样模型即使误传 1/2，也会在 Pydantic 参数校验阶段失败，不会进入 `MemoryManager.write`。本地直接 Memory API 的 sensitivity 1/2 能力不变。

- [ ] **Step 4: 运行验证通过**

在 `src/yuki/cognition/l2/__init__.py` 导出 `AgentLoop`，避免调用方依赖内部文件路径。

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_loop.py tests/cognition/l2/test_bridge.py tests/functions/test_memory_tools.py -v`
Expected: test_loop 与 memory_tools 全 PASS；test_bridge 现状全 PASS（未接线，验证无回归）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/l2/loop.py src/yuki/cognition/l2/__init__.py src/yuki/functions/memory_tools.py tests/cognition/l2/test_loop.py tests/functions/test_memory_tools.py
git commit -m "feat: add AgentLoop multi-step tool loop with transition/interrupt/budget"
```

---

### Task 2: DecisionHub 接入 AgentLoop + 插话中断 + REPLY kind

**Files:**
- Modify: `src/yuki/cognition/l2/bridge.py`（`generate` 委托 AgentLoop，暴露 `loop` 属性）
- Modify: `src/yuki/cognition/brain/hub.py`（`_run_cloud_loop`、探针、crisis 无工具、kind）
- Modify: `src/yuki/config.py`（新增 `AgentLoopConfig`；`cloud.max_turns` 标记弃用）
- Modify: `src/yuki/cognition/assembly.py`（装配 loop_kw）
- Modify: `tests/cognition/test_hub.py`、`tests/cognition/l2/test_bridge.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: `AgentLoop`（Task 1）。
- Produces: `CloudBridge.loop` 属性；`DecisionHub._run_cloud_loop(text, snapshot, *, crisis, publish_reply) -> {"rendered", "spoke", "failed", "interrupted", "reply_id"}`；`DecisionHub.on_user_utterance_probe(topic, payload)`；REPLY 载荷 `kind/reply_id`；`Config.agent_loop`（AgentLoopConfig）。

- [ ] **Step 1: 追加配置测试到 `tests/test_config.py`（先红）**

```python
def test_agent_loop_defaults():
    config = Config()
    assert config.agent_loop.max_steps is None  # None → 回退 cloud.max_turns
    assert config.agent_loop.max_duration_s == 15.0
    assert config.agent_loop.transition_enabled is True
    assert config.agent_loop.transition_fallback == "让我看一下……"
    assert config.agent_loop.transition_grace_s == 0.8
    assert config.agent_loop.tool_result_max_chars == 2000
    assert config.agent_loop.compact_threshold_tokens == 0
    assert config.agent_loop.interrupt_enabled is True


def test_agent_loop_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_AGENT_LOOP_MAX_STEPS", "5")
    monkeypatch.setenv("YUKI_AGENT_LOOP_INTERRUPT_ENABLED", "false")
    config = Config.load(None)
    assert config.agent_loop.max_steps == 5
    assert config.agent_loop.interrupt_enabled is False
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_config.py -v -k "agent_loop"`
Expected: FAIL（`AttributeError: 'Config' object has no attribute 'agent_loop'`）。

- [ ] **Step 3: `src/yuki/config.py` 新增 AgentLoopConfig**

在 `CloudConfig` 之后新增：

```python
class AgentLoopConfig(BaseModel):
    max_steps: int | None = Field(None, ge=1)  # None → 回退 cloud.max_turns（弃用字段）
    max_duration_s: float = Field(15.0, ge=1.0)
    transition_enabled: bool = True
    transition_fallback: str = "让我看一下……"
    transition_grace_s: float = Field(0.8, ge=0.0, le=3.0)
    tool_result_max_chars: int = Field(2000, ge=100)
    compact_threshold_tokens: int = Field(0, ge=0)
    interrupt_enabled: bool = True
```

`CloudConfig.max_turns` 保留但 docstring 标注弃用：`max_turns: int = Field(3, ge=1)  # 弃用：由 agent_loop.max_steps 取代`。

`Config` 中 `cloud` 字段之后新增 `agent_loop: AgentLoopConfig = Field(default_factory=AgentLoopConfig)`；`Config.load` 的 section 元组中 `("cloud", CloudConfig),` 之后新增 `("agent_loop", AgentLoopConfig),`。

- [ ] **Step 4: 改造 `src/yuki/cognition/l2/bridge.py`**

`CloudBridge` 构造新增 `loop_kw`，内部持有 `AgentLoop`：

```python
    def __init__(
        self,
        client: CloudClient,
        registry: FunctionRegistry | None = None,
        system_prompt: str | None = None,
        max_turns: int = 3,
        persona_name: str = "yuki",
        view_builder: CloudViewBuilder | None = None,
        *,
        loop_kw: dict | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._system = system_prompt or DEFAULT_PERSONA_PROMPT.format(persona=persona_name)
        self._summarize = make_summarize(client)
        self._view_builder = view_builder or CloudViewBuilder(summarize=self._summarize)
        loop_options = dict(loop_kw or {})
        loop_options.setdefault("max_steps", max_turns)
        self._loop = AgentLoop(
            client,
            registry=registry,
            system_prompt=self._system,
            view_builder=self._view_builder,
            summarize=self._summarize,
            **loop_options,
        )

    @property
    def loop(self) -> AgentLoop:
        return self._loop

    def set_system_prompt(self, text: str) -> None:
        self._system = text
        self._loop.set_system_prompt(text)
```

`generate` 替换为委托薄壳（保留既有 CloudError 语义，test_bridge 现状断言不变）：

```python
    def generate(self, utterance, context=None, memory=None) -> str:
        try:
            result = self._loop.run(utterance, context, memory)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"generate failed: {exc}") from exc
        if result.get("failed") or result.get("interrupted"):
            raise CloudError("agent loop failed to produce a reply")
        reply = (result.get("text") or "").strip()
        if not reply:
            raise CloudError("empty assistant reply")
        return reply
```

`import` 区新增 `from yuki.cognition.l2.loop import AgentLoop, make_summarize`；删除原 `generate` 循环体、重复的 `SUMMARIZE_PROMPT` 与 `_summarize_closure`。`refine_persona` 的 `REFINE_PROMPT`/5s timeout 保持不变。

- [ ] **Step 5: 改造 `src/yuki/cognition/brain/hub.py`（接入 loop + 探针 + kind）**

- import 区新增 `import math`、`import uuid`；`reply_id` 由 Hub 为每次回复流程生成，AgentLoop 不依赖 Bus 协议。

- `DecisionHub.__init__` 新增参数 `loop=None, transition_enabled: bool = True`，新增字段：

```python
        self._loop = loop or (getattr(bridge, "loop", None) if bridge is not None else None)
        self._pending_input_ts = 0.0
        self._probe_lock = threading.Lock()
        self._transition_enabled = bool(transition_enabled)
```

- 新增探针方法（**不取 `_decision_lock`**）：

```python
    def on_user_utterance_probe(self, topic: str, payload: dict) -> None:
        """不取 decision lock 的插话探针：记录合法输入时间戳，供 loop 边界检查。"""
        ts = payload.get("ts")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or not math.isfinite(float(ts))
        ):
            logger.warning("ignoring utterance probe without numeric ts")
            return
        with self._probe_lock:
            if ts > self._pending_input_ts:
                self._pending_input_ts = ts
```

- 用 `_run_cloud_loop` 替换 `_try_cloud`：

```python
    def _run_cloud_loop(
        self,
        text: str,
        snapshot: ContextSnapshot | None,
        *,
        crisis: bool = False,
        publish_reply: bool = True,
    ) -> dict:
        """执行云端 Agent Loop；同一轮 transition/final/cancel 共享 reply_id。"""
        reply_id = uuid.uuid4().hex
        if self._loop is None:
            return {"rendered": "", "spoke": False, "failed": True,
                    "interrupted": False, "reply_id": reply_id}
        started = time.time()
        transition_sent = [False]

        def on_transition(transition: str) -> None:
            if transition_sent[0]:
                return
            transition_sent[0] = True
            self._bus.publish(Topics.REPLY, {
                "text": transition,
                "ts": time.time(),
                "emotion": "neutral",
                "kind": "transition",
                "reply_id": reply_id,
            })

        def cancel_transition() -> None:
            if publish_reply and transition_sent[0]:
                self._bus.publish(Topics.REPLY, {
                    "text": "", "ts": time.time(), "emotion": "neutral",
                    "kind": "cancel", "reply_id": reply_id,
                })

        def interrupt_check() -> bool:
            with self._probe_lock:
                return self._pending_input_ts > started

        try:
            result = self._loop.run(
                text,
                snapshot,
                self._memory,
                crisis=crisis,
                on_transition=(
                    on_transition if publish_reply and self._transition_enabled else None
                ),
                # Gateway/chat 请求不属于语音回合，不受 USER_UTTERANCE probe 打断。
                interrupt_check=interrupt_check if publish_reply else None,
            )
        except Exception:
            logger.warning("agent loop failed", exc_info=True)
            return {"rendered": "", "spoke": False, "failed": True,
                    "interrupted": False, "reply_id": reply_id}
        if result.get("failed"):
            return {"rendered": "", "spoke": False, "failed": True,
                    "interrupted": False, "reply_id": reply_id}
        if result.get("interrupted"):
            cancel_transition()
            return {"rendered": "", "spoke": False, "failed": False,
                    "interrupted": True, "reply_id": reply_id}
        reply = (result.get("text") or "").strip()
        if not reply:
            return {"rendered": "", "spoke": False, "failed": True,
                    "interrupted": False, "reply_id": reply_id}
        return {"rendered": reply, "spoke": True, "failed": False,
                "interrupted": False, "reply_id": reply_id}
```

- `_handle_utterance` 危机分支改为：

```python
        if is_crisis(text):
            result = self._run_cloud_loop(text, snapshot, crisis=True, publish_reply=publish_reply)
            if result["failed"] or result["interrupted"] or not result["spoke"]:
                rendered, spoke = CRISIS_FALLBACK_REPLY, True
            else:
                rendered, spoke = result["rendered"], True
            return self._result(
                rendered, spoke, reason="crisis", route=LocalRoute.CLOUD,
                emotion=Emotion.SADNESS, reply_id=result["reply_id"],
            )
```

- `_handle_locked` 中 `self._handle_utterance(text, snapshot, effective_situation)` → `self._handle_utterance(text, snapshot, effective_situation, publish_reply=publish_reply)`；`_handle_utterance`/`_dispatch_chat_local`/`_dispatch_tool_local`/`_dispatch_vision`/`_cloud_or_notice` 签名增加 `publish_reply` 并透传（`_cloud_or_notice` 内部改为调 `_run_cloud_loop`，interrupted → Hub 必要时先发 cancel，再返回 `_result("", False, reason="interrupted", route=LocalRoute.CLOUD)`，不发 final；failed/空 → `L2_UNAVAILABLE_NOTICE`）。
- `_run_cloud_loop` 返回的 `reply_id` 经 `_cloud_or_notice`/危机分支传入 `_result`；本地回复与 situation 回复由 `_handle_locked` 生成新 `reply_id`。最终 REPLY 发布载荷增加 `"kind": "final", "reply_id": result_reply_id`，`DecisionTrace` 同步记录 reply_id 便于关联 transition/cancel/final。
- `build_brain`：`DecisionHub(...)` 传入 `transition_enabled=cfg.agent_loop.transition_enabled`；订阅探针：

```python
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    if cfg.agent_loop.interrupt_enabled:
        bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance_probe)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
```

（Bus 每个 handler 独立 worker 线程，探针与主 handler 并发运行，天然绕过 `_decision_lock`。生产 `UserUtterancePayload.ts` 在发布时生成，因此启动本 loop 的输入 `ts < started`；缺失/非法 ts 被忽略，避免消费时刻导致自中断。）

- [ ] **Step 6: `src/yuki/cognition/assembly.py` 装配 loop 配置**

`_build_bridge` 中 `CloudBridge(...)` 增加：

```python
            max_turns=(
                cfg.agent_loop.max_steps
                if cfg.agent_loop.max_steps is not None
                else cfg.cloud.max_turns
            ),
            loop_kw={
                "max_duration_s": cfg.agent_loop.max_duration_s,
                "tool_result_max_chars": cfg.agent_loop.tool_result_max_chars,
                "compact_threshold_tokens": cfg.agent_loop.compact_threshold_tokens,
                "transition_fallback": cfg.agent_loop.transition_fallback,
            },
```

- [ ] **Step 7: 更新测试**

- `tests/cognition/l2/test_bridge.py`：`generate` 返回/异常语义保持；因新增独立的偏好 system message，把固定 `messages[1]` 的 user 位置断言改为按 `role == "user"` 查找，并断言第一条 system 仍逐字等于调用方提供的 prompt、第二条 system 含 `memory.write` 约束。`test_set_system_prompt_updates` 同时断言 `bridge.loop._system == "新的系统提示"`。
- `tests/cognition/test_hub.py`：新增 FakeLoop 与用例：

```python
import time


class FakeLoop:
    def __init__(self, result=None, transition=None, check_interrupt=False):
        self.calls = []
        self.result = result or {"text": "云端回复", "steps": 1, "interrupted": False, "failed": False}
        self.transition = transition
        self.check_interrupt = check_interrupt

    def run(self, utterance, context=None, memory=None, *, crisis=False,
            on_transition=None, interrupt_check=None):
        self.calls.append({"utterance": utterance, "crisis": crisis,
                           "on_transition": on_transition, "interrupt_check": interrupt_check})
        if self.check_interrupt and interrupt_check is not None and interrupt_check():
            return {"text": "", "steps": 0, "interrupted": True, "failed": False}
        if self.transition is not None and on_transition is not None:
            on_transition(self.transition)
        return dict(self.result)


def test_cloud_utterance_runs_loop_and_publishes_final(hub):
    h, bus, _ = hub
    loop = FakeLoop()
    h._loop = loop
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": 1.0})
    assert loop.calls and loop.calls[0]["utterance"] == "查一下"
    assert loop.calls[0]["crisis"] is False
    last = [p for p in bus.published if p[0] == Topics.REPLY][-1]
    assert last[1]["text"] == "云端回复"
    assert last[1]["kind"] == "final"
    assert last[1]["reply_id"]


def test_crisis_runs_loop_with_crisis_flag_and_falls_back(hub):
    h, bus, _ = hub
    loop = FakeLoop(result={"text": "", "steps": 1, "interrupted": False, "failed": True})
    h._loop = loop
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我不想活了", "ts": 1.0})
    assert loop.calls[0]["crisis"] is True
    last = [p for p in bus.published if p[0] == Topics.REPLY][-1]
    assert "求助" in last[1]["text"]


def test_crisis_interrupt_is_explicit_exception_and_still_falls_back(hub):
    h, bus, _ = hub
    h._loop = FakeLoop(
        result={"text": "", "steps": 1, "interrupted": True, "failed": False},
    )
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我想死", "ts": 1.0})
    replies = [p[1] for p in bus.published if p[0] == Topics.REPLY]
    assert [p["kind"] for p in replies] == ["final"]
    assert replies[0]["text"] == CRISIS_FALLBACK_REPLY


def test_transition_published_with_kind(hub):
    h, bus, _ = hub
    h._loop = FakeLoop(transition="让我看看")
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": 1.0})
    replies = [p[1] for p in bus.published if p[0] == Topics.REPLY]
    kinds = [p["kind"] for p in replies]
    assert kinds == ["transition", "final"]
    assert replies[0]["reply_id"] == replies[1]["reply_id"]


def test_interrupted_loop_cancels_published_transition(hub):
    h, bus, _ = hub
    loop = FakeLoop(
        transition="让我看看",
        result={"text": "", "steps": 1, "interrupted": True, "failed": False},
    )
    h._loop = loop
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": 1.0})
    replies = [p[1] for p in bus.published if p[0] == Topics.REPLY]
    assert [p["kind"] for p in replies] == ["transition", "cancel"]
    assert replies[0]["reply_id"] == replies[1]["reply_id"]


def test_probe_marks_pending_input_and_interrupts(hub):
    h, bus, _ = hub

    class ProbeDuringRun(FakeLoop):
        def run(self, *args, interrupt_check=None, **kwargs):
            h.on_user_utterance_probe(
                Topics.USER_UTTERANCE, {"text": "喂", "ts": time.time() + 1.0},
            )
            self.check_interrupt = True
            return super().run(*args, interrupt_check=interrupt_check, **kwargs)

    h._loop = ProbeDuringRun()
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": 1.0})
    # transition 尚未发布，interrupted 路径完全静默
    replies = [p for p in bus.published if p[0] == Topics.REPLY]
    assert replies == []


def test_probe_ignores_missing_timestamp(hub):
    h, _, _ = hub
    h.on_user_utterance_probe(Topics.USER_UTTERANCE, {"text": "无时间戳"})
    assert h._pending_input_ts == 0.0


def test_non_publishing_chat_loop_is_not_interrupted_by_voice_probe(hub):
    h, bus, _ = hub
    loop = FakeLoop(check_interrupt=True)
    h._loop = loop
    h.on_user_utterance_probe(
        Topics.USER_UTTERANCE, {"text": "语音输入", "ts": time.time() + 1.0},
    )
    result = h._run_cloud_loop(
        "桌面聊天请求", ContextSnapshot(), publish_reply=False,
    )
    assert loop.calls[0]["interrupt_check"] is None
    assert result["rendered"] == "云端回复"
    assert result["interrupted"] is False
    assert not any(topic == Topics.REPLY for topic, _ in bus.published)
```

- [ ] **Step 8: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/l2/test_loop.py tests/cognition/l2/test_bridge.py tests/cognition/test_hub.py tests/test_config.py -v`
Expected: 全 PASS（`test_config.py -k "agent_loop"` 计入）。

- [ ] **Step 9: Commit**

```bash
git add src/yuki/cognition/l2/bridge.py src/yuki/cognition/brain/hub.py src/yuki/config.py src/yuki/cognition/assembly.py tests/cognition/l2/test_bridge.py tests/cognition/test_hub.py tests/test_config.py
git commit -m "feat: route cloud path through AgentLoop with transitions, interrupt probe, REPLY kind"
```

---

### Task 3: 移除 Sedimenter、简化 Tuner、persona 刷新替代

**Files:**
- Delete: `src/yuki/cognition/brain/sedimenter.py`、`tests/cognition/test_sedimenter.py`
- Modify: `src/yuki/cognition/brain/tuner.py`（重写）、`src/yuki/cognition/brain/sink.py`、`src/yuki/cognition/brain/soul.py`、`src/yuki/cognition/brain/hub.py`、`src/yuki/cognition/assembly.py`、`src/yuki/config.py`、`config.example.yaml`
- Modify: `tests/cognition/test_tuner.py`、`tests/cognition/test_sink.py`、`tests/cognition/test_soul.py`、`tests/cognition/test_hub.py`、`tests/cognition/test_cognition.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: `DecisionPolicy`、`TunerStateStore`。
- Produces: 简化版 `FeedbackTuner(policy, state, *, window_s, cooldown_min_s, cooldown_max_s, floor_step_s, floor_negatives)`（无 soul 参数、无 traits）；`DecisionSink` 协议去掉 `on_engagement`；`DecisionHub` 新增 `periodic`/`periodic_interval` 回调（单 daemon worker，触发计数不丢失）；`Config.persona.refresh_every_utterances`；`soul.py` 删除 `FLOOR_KEY` 之外的死代码并新增 `FLOOR_KEY = "cooldown_floor_s"`。

- [ ] **Step 1: 重写 `src/yuki/cognition/brain/tuner.py`（先写测试再实现）**

`tests/cognition/test_tuner.py` 改写为：

```python
import time

import pytest

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import COOLDOWN_KEY, FLOOR_KEY, TunerStateStore
from yuki.cognition.brain.tuner import FeedbackTuner, detect_polarity


def _tuner(tmp_path, **kw):
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    state = TunerStateStore(tmp_path / "tuner_state.json", "yuki")
    return FeedbackTuner(policy, state, **kw), policy, state


def test_detect_polarity_keywords():
    assert detect_polarity("你话太多了") == "negative"
    assert detect_polarity("说得好") == "positive"
    assert detect_polarity("今天天气不错") == "neutral"


def test_negative_feedback_increases_cooldown(tmp_path):
    tuner, policy, _ = _tuner(tmp_path)
    tuner.on_user_utterance("太吵了")
    assert policy.cooldown_s == pytest.approx(120.0 * 1.5)


def test_positive_feedback_decreases_cooldown(tmp_path):
    tuner, policy, _ = _tuner(tmp_path)
    tuner.on_user_utterance("说得好")
    assert policy.cooldown_s == pytest.approx(120.0 * 0.8)


def test_repeated_negative_raises_floor(tmp_path):
    tuner, policy, state = _tuner(tmp_path, floor_step_s=30.0, floor_negatives=2)
    tuner.on_user_utterance("太吵了")
    tuner.on_user_utterance("话太多")
    assert policy.cooldown_s >= 30.0 + 30.0  # floor 抬升后 cooldown 不低于新下限
    saved = state.load()
    assert saved[FLOOR_KEY] == 60.0


def test_load_restores_cooldown_and_floor(tmp_path):
    tuner, policy, state = _tuner(tmp_path)
    state.save({COOLDOWN_KEY: 60.0, FLOOR_KEY: 90.0})
    tuner.load_soul()
    assert tuner.cooldown_s == 90.0
    assert policy.cooldown_s == 90.0
    # 后续调整也不得低于已恢复 floor
    tuner.adjust(0.1)
    assert policy.cooldown_s >= 90.0


def test_fast_reply_after_proactive_open_reduces_cooldown(tmp_path, monkeypatch):
    tuner, policy, _ = _tuner(tmp_path)
    now = [100.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    tuner.on_proactive_open()
    now[0] += 5.0
    tuner.on_user_utterance("嗯嗯")
    assert policy.cooldown_s == pytest.approx(120.0 * 0.9)
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py -v`
Expected: FAIL（旧实现无 floor 语义/无 `FLOOR_KEY`）。

- [ ] **Step 3: 重写 `src/yuki/cognition/brain/tuner.py`**

```python
import time

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import COOLDOWN_KEY, FLOOR_KEY, TunerStateStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.tuner")

NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")


def detect_polarity(text: str) -> str:
    lowered = (text or "").lower()
    if any(kw in lowered for kw in NEGATIVE_KEYWORDS):
        return "negative"
    if any(kw in lowered for kw in POSITIVE_KEYWORDS):
        return "positive"
    return "neutral"


class FeedbackTuner:
    """环1 参数自调（简化版）：反馈只调主动开口 cooldown。

    traits 调整已移除：行为风格由云端模型经 Soul 注入的 system prompt 自主管理，
    本地通道使用静态 traits。cooldown 下限由重复负面反馈自举（替代原 Sedimenter
    的 set_cooldown_floor 机制）。
    """

    def __init__(
        self,
        policy: DecisionPolicy,
        state: TunerStateStore,
        *,
        window_s: float = 90.0,
        cooldown_min_s: float = 30.0,
        cooldown_max_s: float = 600.0,
        floor_step_s: float = 30.0,
        floor_negatives: int = 3,
    ) -> None:
        self._policy = policy
        self._state = state
        self._window_s = window_s
        self._min_s = cooldown_min_s
        self._max_s = cooldown_max_s
        self._floor_step_s = floor_step_s
        self._floor_negatives = max(1, floor_negatives)
        self._open_ts = None
        self._negatives = 0
        self._cooldown = policy.cooldown_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def load_soul(self) -> None:
        params = self._state.load()
        if not params:
            return
        if isinstance(params.get(FLOOR_KEY), (int, float)):
            self._min_s = min(max(float(params[FLOOR_KEY]), self._min_s), self._max_s)
        restored = params.get(COOLDOWN_KEY, self._cooldown)
        if isinstance(restored, (int, float)):
            self._cooldown = min(max(float(restored), self._min_s), self._max_s)
            self._policy.set_cooldown_s(self._cooldown)

    def on_proactive_open(self) -> None:
        self._open_ts = time.time()

    def on_user_utterance(self, text: str) -> None:
        self._check_timeout()
        polarity = detect_polarity(text)
        if polarity == "negative":
            self._negatives += 1
            self.adjust(1.5)
            if self._negatives >= self._floor_negatives:
                self._negatives = 0
                self._raise_floor()
            self._open_ts = None
            return
        if polarity == "positive":
            self._negatives = 0
            self.adjust(0.8)
            self._open_ts = None
            return
        if self._open_ts is not None and time.time() - self._open_ts <= self._window_s:
            self.adjust(0.9)
            self._open_ts = None

    def _check_timeout(self) -> None:
        if self._open_ts is not None and time.time() - self._open_ts > self._window_s:
            self.adjust(1.3)
            self._open_ts = None

    def _raise_floor(self) -> None:
        self._min_s = min(self._min_s + self._floor_step_s, self._max_s)
        if self._cooldown < self._min_s:
            self._cooldown = self._min_s
            self._policy.set_cooldown_s(self._cooldown)
        self._state.save({COOLDOWN_KEY: self._cooldown, FLOOR_KEY: self._min_s})

    def adjust(self, factor: float) -> None:
        new = min(max(self._cooldown * factor, self._min_s), self._max_s)
        if new == self._cooldown:
            return
        self._cooldown = new
        self._policy.set_cooldown_s(new)
        self._state.save({COOLDOWN_KEY: new})
        logger.info("tuned cooldown", cooldown_s=new, factor=factor)
```

- [ ] **Step 4: `src/yuki/cognition/brain/soul.py` 清理 + `FLOOR_KEY`**

- `COOLDOWN_KEY` 旁新增：`FLOOR_KEY = "cooldown_floor_s"`。
- 删除死代码：`adjust_traits`、`on_preference_sedimented`、`reset_prefs_since_regen`、`apply_core_value_feedback`、`_promote_catalogued_core_value`、`_extract_core_value_replacement`、`_adjust_trait_values`、`TRAIT_CENTER`、`TRAIT_CENTER_STEP`、`CORE_VALUE_CATALOG`、`CORE_VALUE_OPPOSITION_KEYWORDS`、`PREFS_PER_PERSONA_REGEN`。
- 保留：`DEFAULT_TRAITS`（静态渲染）、`snapshot()`、`set_personality_description`（供未来手动编辑）、`prefs_since_regen` 字段（`_normalize` 容忍读取，不再写入）。
- `tests/cognition/test_soul.py`：删除引用上述方法的用例，其余保留。

- [ ] **Step 5: `src/yuki/cognition/brain/sink.py` 瘦身**

```python
from typing import Protocol


class DecisionSink(Protocol):
    """hub 决策事件的下游消费者（当前仅 Tuner）。"""

    def on_proactive_open(self) -> None: ...

    def on_user_utterance(self, text: str) -> None: ...


class TunerSink:
    def __init__(self, tuner) -> None:
        self._tuner = tuner

    def on_proactive_open(self) -> None:
        self._tuner.on_proactive_open()

    def on_user_utterance(self, text: str) -> None:
        self._tuner.on_user_utterance(text)
```

删除 `SedimenterSink`、`on_engagement`、`Intent` import。`tests/cognition/test_sink.py` 同步改写（仅 TunerSink 转发用例）。

- [ ] **Step 6: `src/yuki/cognition/brain/hub.py` 移除 sedimenter + 加 periodic**

- `DecisionHub.__init__`：删除 `sedimenter` 参数与 `SedimenterSink` 注册；新增：

```python
        self._periodic: list = list(periodic or [])
        self._periodic_interval = max(0, int(periodic_interval or 0))
        self._utterance_count = 0
        self._periodic_running = False
        self._periodic_pending = 0
        self._periodic_lock = threading.Lock()
```

- `_handle_locked` 的 sink 段替换为（删除 trusted_metadata 门与 `on_engagement`）：

```python
        if trigger == TriggerKind.SITUATION and spoke:
            for sink in self._sinks:
                sink.on_proactive_open()
        if trigger == TriggerKind.UTTERANCE:
            for sink in self._sinks:
                sink.on_user_utterance(text)
            self._utterance_count += 1
            if self._periodic_interval > 0 and self._utterance_count % self._periodic_interval == 0:
                self._run_periodic()
```

- 新增（daemon 线程执行，避免云端精修阻塞 `_decision_lock`；running 守卫防重入）：

```python
    def _run_periodic(self) -> None:
        with self._periodic_lock:
            self._periodic_pending += 1
            if self._periodic_running:
                return
            self._periodic_running = True

        def worker() -> None:
            while True:
                with self._periodic_lock:
                    if self._periodic_pending <= 0:
                        self._periodic_running = False
                        return
                    self._periodic_pending -= 1
                for callback in self._periodic:
                    try:
                        callback()
                    except Exception:
                        logger.warning("periodic callback failed", exc_info=True)

        threading.Thread(target=worker, daemon=True, name="yuki-periodic").start()
```

- `build_brain` 签名删除 `sedimenter`，新增 `periodic=None, periodic_interval=0` 并传入 hub。

- [ ] **Step 7: `src/yuki/cognition/assembly.py` 重写 persona_refresh + 删 sedimenter**

- 删除 `PreferenceSedimenter` 构造与 `on_sedimented=persona_refresh` 接线；`build_brain(...)` 调用删除 `sedimenter=...`。
- `_build_persona_refresh` 改为无参刷新（保留 description 静态，prompt 随当前偏好再生）：

```python
    def _build_persona_refresh(
        self,
        memory: MemoryManager,
        bridge: CloudBridge | None,
        persona_store: PersonaStore,
        soul_store: SoulStore,
        local_composer,
    ) -> Callable[[], None]:
        def persona_refresh() -> None:
            soul = soul_store.load_or_default()
            prefs = MemoryAccess(memory).list(
                purpose=MemoryPurpose.PERSONA_REFINE_CLOUD,
                memory_type="preference",
            )
            refine = bridge.refine_persona if self.config.persona.enable_llm_refine and bridge else None
            prompt = generate_persona(
                self.config.persona_name,
                prefs,
                {},
                base_prompt=self.config.persona.prompt,
                refine=refine,
                soul=soul,
            )
            snap = persona_store.save(prompt, {}, soul=soul_store.snapshot())
            effective_prompt = snap.persona_prompt if snap is not None else prompt
            if bridge is not None:
                bridge.set_system_prompt(effective_prompt)
            if local_composer is not None:
                local_composer.set_system_prompt(effective_prompt)
        return persona_refresh
```

（`set_system_prompt` 同时更新 bridge 内部 `AgentLoop` 的 system prompt——Task 2 已保证。）

- `assemble()` 中 `build_brain(...)` 增加：

```python
            periodic=[persona_refresh],
            periodic_interval=self.config.persona.refresh_every_utterances,
```

- `Tuner` 构造去掉 `soul=soul_store` 参数。

- [ ] **Step 8: `src/yuki/config.py` 配置变更**

- `PersonaConfig` 新增 `refresh_every_utterances: int = Field(30, ge=1)`，名称明确表示按用户 utterance 数量而非秒数。
- 删除 `SedimenterConfig` 类及 `Config` 中 `sedimenter` 字段、`Config.load` section 元组中的 `("sedimenter", SedimenterConfig)`。
- `tests/test_config.py`：删 `test_sedimenter_*`；新增 `test_persona_refresh_every_utterances_default`。

- [ ] **Step 9: 删除 sedimenter 文件并更新测试**

- `Remove-Item src/yuki/cognition/brain/sedimenter.py, tests/cognition/test_sedimenter.py`
- `tests/cognition/test_hub.py`：删除 `FakeSedimenter` 及 `test_sedimenter_only_receives_trusted_router_metadata`、`test_sedimenter_skips_router_failure_metadata`；新增 periodic 用例：

```python
def test_periodic_callback_does_not_drop_trigger_while_running(hub):
    import threading

    h, bus, _ = hub
    fired = []
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def callback():
        fired.append(1)
        if len(fired) == 1:
            first_started.set()
            assert release_first.wait(1.0)
        if len(fired) == 2:
            second_finished.set()

    h._periodic = [callback]
    h._periodic_interval = 2
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "一", "ts": 1.0})
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "二", "ts": 2.0})
    assert first_started.wait(1.0)
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "三", "ts": 3.0})
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "四", "ts": 4.0})
    release_first.set()
    assert second_finished.wait(1.0)
    assert len(fired) == 2
```

- `tests/cognition/test_cognition.py`：删除 sedimenter 构建/断言（`test_cognition_agent_builds_sedimenter`、`agent._hub._sedimenter.*` 相关行）。
- `tests/cognition/test_assembly.py`：适配无 sedimenter 装配。
- `config.example.yaml`：删除 `sedimenter:` 节，`persona:` 节加 `refresh_every_utterances: 30`。

- [ ] **Step 10: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py tests/cognition/test_sink.py tests/cognition/test_soul.py tests/cognition/test_hub.py tests/cognition/test_cognition.py tests/cognition/test_assembly.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: remove Sedimenter, simplify Tuner to cooldown-only, interval persona refresh"
```

---

### Task 4: L0 守门员二分类（移除 Intent / 4 路由）

**Files:**
- Modify: `src/yuki/cognition/brain/classifier.py`、`src/yuki/cognition/brain/local/router.py`、`src/yuki/cognition/brain/hub.py`、`src/yuki/cognition/brain/__init__.py`、`src/yuki/cognition/assembly.py`、`src/yuki/config.py`
- Delete: `src/yuki/cognition/brain/route.py`、`tests/cognition/test_route.py`
- Modify: `tests/cognition/test_local_router.py`、`tests/cognition/test_hub.py`、`tests/cognition/test_assembly.py`、`tests/test_config.py`
- Create: `tests/cognition/test_classifier.py`

**Interfaces:**
- Consumes: `Emotion`（保留）、`GateRoute`。
- Produces: `GateRoute(LOCAL/CLOUD)`；`RouterDecision(route, confidence, reason)`（去掉 intent/emotion/tool_call/trusted_metadata）；`is_explicit_preference(text)` 规则层保证显式偏好进入 cloud；`detect_emotion(text) -> Emotion`；`DecisionHub._dispatch_local`；`build_brain` 去掉 `vision_screen`/`local_tool_allowlist`。`route.py`/`DecisionRouter` 删除。

- [ ] **Step 1: 创建 `tests/cognition/test_classifier.py`（先红）**

```python
from yuki.cognition.brain.classifier import Emotion, detect_emotion


def test_emotion_keyword_detection():
    assert detect_emotion("太开心了") == Emotion.JOY
    assert detect_emotion("我今天很难过") == Emotion.SADNESS
    assert detect_emotion("压力好大") == Emotion.ANXIETY
    assert detect_emotion("气死我了") == Emotion.ANGER
    assert detect_emotion("想你了") == Emotion.LOVE
    assert detect_emotion("好累") == Emotion.TIRED


def test_emotion_neutral_fallback():
    assert detect_emotion("随便聊聊") == Emotion.NEUTRAL
    assert detect_emotion("") == Emotion.NEUTRAL
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_classifier.py -v`
Expected: FAIL（`ImportError: cannot import name 'detect_emotion'`）。

- [ ] **Step 3: 重写 `src/yuki/cognition/brain/classifier.py`**

```python
from enum import Enum


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    JOY = "joy"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANGER = "anger"
    LOVE = "love"
    TIRED = "tired"


EMOTION_KEYWORDS: dict[Emotion, tuple[str, ...]] = {
    Emotion.JOY: ("开心", "高兴", "好棒", "太棒了", "哈哈", "真棒", "耶"),
    Emotion.SADNESS: ("难过", "伤心", "想哭", "委屈", "失落", "哭了"),
    Emotion.ANXIETY: ("焦虑", "紧张", "压力", "害怕", "担心", "不安"),
    Emotion.ANGER: ("生气", "气死", "烦死了", "讨厌", "恼火"),
    Emotion.LOVE: ("想你", "爱你", "喜欢你", "抱抱"),
    Emotion.TIRED: ("好累", "累死", "疲惫", "困"),
}


def detect_emotion(text: str) -> Emotion:
    """轻量关键词情绪检测：用于本地回复与成功云端回复的 emotion 字段。"""
    lowered = (text or "").lower()
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return emotion
    return Emotion.NEUTRAL
```

`Intent` 枚举删除。`src/yuki/cognition/brain/__init__.py` 改为导出 `Emotion, detect_emotion`。

- [ ] **Step 4: 重写 `src/yuki/cognition/brain/local/router.py`（2 分类）**

```python
import json
from contextlib import nullcontext
from dataclasses import dataclass
from enum import StrEnum

from yuki.cognition.model_registry import ModelRegistry
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.local.router")

CRISIS_KEYWORDS = ("自杀", "自伤", "不想活", "想死", "活着没意思", "想结束生命", "割腕")
EXPLICIT_PREFERENCE_MARKERS = (
    "我喜欢", "我不喜欢", "以后请", "以后不要", "请记住",
    "别再", "不要再", "说反了", "简短一点", "更简短", "温柔一点",
)


class GateRoute(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True)
class RouterDecision:
    route: GateRoute
    confidence: float
    reason: str = ""

    @classmethod
    def cloud(cls, reason: str = "fallback") -> "RouterDecision":
        return cls(GateRoute.CLOUD, 0.0, reason=reason)


class LocalRouter:
    """L0 守门员：1.7B 模型二分类 local/cloud；危机关键词规则层先行拦截。"""

    def __init__(
        self,
        model,
        *,
        threshold: float = 0.7,
        retry: int = 1,
        prompt_max_tokens: int = 1200,
        timeout_ms: int = 150,
        model_registry: ModelRegistry | None = None,
        model_name: str = "local_chat",
    ) -> None:
        self._model = model
        self._model_registry = model_registry
        self._model_name = model_name
        self._threshold = threshold
        self._retry = retry
        self._prompt_max_tokens = prompt_max_tokens
        self._timeout_ms = timeout_ms

    def warmup(self) -> None:
        if hasattr(self._model, "warmup"):
            self._model.warmup()

    def route(self, text: str, *, snapshot=None, situation: dict | None = None) -> RouterDecision:
        if is_crisis(text):
            return RouterDecision(GateRoute.CLOUD, 1.0, reason="crisis")
        if is_explicit_preference(text):
            return RouterDecision(GateRoute.CLOUD, 1.0, reason="explicit_preference")
        messages = self._messages(text, snapshot=snapshot, situation=situation)
        raw = ""
        for attempt in range(max(0, self._retry) + 1):
            try:
                with self._model_call_tracker():
                    raw = self._model.generate(
                        messages,
                        max_new_tokens=60,
                        timeout_ms=self._timeout_ms,
                    )
                    return self._parse_and_validate(raw)
            except Exception:
                logger.warning("local router failed", attempt=attempt, raw=raw, exc_info=True)
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": "上一次输出无效。只输出一个严格 JSON 对象，不要解释。",
                    },
                ]
        return RouterDecision.cloud("router_failed")

    def _model_call_tracker(self):
        if self._model_registry is None:
            return nullcontext()
        return self._model_registry.track_call(self._model_name)

    def _messages(self, text: str, *, snapshot=None, situation: dict | None = None) -> list[dict]:
        recent = []
        if snapshot is not None:
            for turn in list(getattr(snapshot, "recent_turns", ()) or ())[:3]:
                recent.append({
                    "kind": turn.get("kind", "turn"),
                    "content": str(turn.get("content", ""))[:200],
                })
        payload = {
            "utterance": text,
            "recent_turns": recent,
            "situation": situation or getattr(snapshot, "situation", None) or {},
            "routes": [item.value for item in GateRoute],
        }
        user = json.dumps(payload, ensure_ascii=False)
        max_chars = int(self._prompt_max_tokens * 1.5)
        if len(user) > max_chars:
            user = user[:max_chars]
        return [
            {
                "role": "system",
                "content": (
                    "你是本地低延迟守门员。只输出严格 JSON，字段为 route、confidence。"
                    'route 只能取 "local" 或 "cloud"：local 表示简单对话/闲聊/情感回应，'
                    "本地模型即可自然回复；cloud 表示需要查信息、执行命令、多步推理、复杂问题，"
                    "以及任何需要长期记住的显式用户偏好或纠正。"
                ),
            },
            {"role": "user", "content": user},
        ]

    def _parse_and_validate(self, raw: str) -> RouterDecision:
        data = _parse_json_object(raw)
        route = GateRoute(str(data.get("route", "")))
        confidence = float(data.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence out of range")
        if confidence < self._threshold:
            return RouterDecision.cloud("low_confidence")
        return RouterDecision(route, confidence, reason="router")


def is_crisis(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in CRISIS_KEYWORDS)


def is_explicit_preference(text: str) -> bool:
    normalized = (text or "").strip()
    return any(marker in normalized for marker in EXPLICIT_PREFERENCE_MARKERS)


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("router output must be object")
    return data
```

（签名去掉 `registry`/`local_tool_allowlist`；`tool_summaries`、`_validate_tool_call` 删除。）

- [ ] **Step 5: 重写 `tests/cognition/test_local_router.py`**

```python
import pytest

from yuki.cognition.brain.local.router import GateRoute, LocalRouter, RouterDecision, is_crisis


class FakeModel:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    def generate(self, messages, max_new_tokens=None, timeout_ms=None):
        self.calls.append(messages)
        return self._outputs.pop(0)


def _decision(router, text):
    return router.route(text)


def test_crisis_short_circuits_to_cloud():
    router = LocalRouter(FakeModel([]))
    decision = _decision(router, "我不想活了")
    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "crisis"


def test_explicit_preference_short_circuits_to_cloud():
    model = FakeModel([])
    router = LocalRouter(model)
    decision = _decision(router, "以后请回答简短一点")
    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "explicit_preference"
    assert model.calls == []


def test_local_route_above_threshold():
    router = LocalRouter(FakeModel(['{"route": "local", "confidence": 0.9}']))
    decision = _decision(router, "今天好累")
    assert decision.route == GateRoute.LOCAL
    assert decision.confidence == 0.9


def test_cloud_route_above_threshold():
    router = LocalRouter(FakeModel(['{"route": "cloud", "confidence": 0.8}']))
    decision = _decision(router, "帮我查一下明天的天气")
    assert decision.route == GateRoute.CLOUD


def test_low_confidence_falls_back_to_cloud():
    router = LocalRouter(FakeModel(['{"route": "local", "confidence": 0.5}']), threshold=0.7)
    decision = _decision(router, "随便")
    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "low_confidence"


def test_retry_recovers_from_bad_output():
    router = LocalRouter(
        FakeModel(["不是json", '{"route": "local", "confidence": 0.9}']),
        retry=1,
    )
    decision = _decision(router, "你好")
    assert decision.route == GateRoute.LOCAL


def test_router_failure_returns_cloud():
    router = LocalRouter(FakeModel(["坏输出"]), retry=0)
    decision = _decision(router, "你好")
    assert decision.route == GateRoute.CLOUD
    assert decision.reason == "router_failed"


def test_is_crisis_keywords():
    assert is_crisis("活着没意思")
    assert not is_crisis("有意思")
```

- [ ] **Step 6: `src/yuki/cognition/brain/hub.py` 路由简化**

- import：`LocalRoute` → `GateRoute`（`from yuki.cognition.brain.local.router import GateRoute, RouterDecision, is_crisis`）；`classifier` import 改 `from yuki.cognition.brain.classifier import Emotion, detect_emotion`；删除 `route.py`/`sink.py` 中 `SedimenterSink` import。
- `_handle_utterance` 重写（`publish_reply` 透传已在 Task 2）：

```python
    def _handle_utterance(
        self,
        text: str,
        snapshot: ContextSnapshot,
        situation: dict | None,
        *,
        publish_reply: bool,
    ) -> dict:
        if is_crisis(text):
            result = self._run_cloud_loop(text, snapshot, crisis=True, publish_reply=publish_reply)
            if result["failed"] or result["interrupted"] or not result["spoke"]:
                rendered, spoke = CRISIS_FALLBACK_REPLY, True
            else:
                rendered, spoke = result["rendered"], True
            return self._result(
                rendered, spoke, reason="crisis", route=GateRoute.CLOUD,
                emotion=Emotion.SADNESS, reply_id=result["reply_id"],
            )

        if not self._local_enabled or self._local_router is None:
            return self._cloud_or_notice(text, snapshot, reason="cloud", publish_reply=publish_reply)

        decision = self._local_router.route(text, snapshot=snapshot, situation=situation)
        if decision.route == GateRoute.CLOUD:
            return self._cloud_or_notice(
                text, snapshot, decision=decision, reason="cloud", publish_reply=publish_reply,
            )
        return self._dispatch_local(text, snapshot, decision, publish_reply=publish_reply)

    def _dispatch_local(self, text: str, snapshot: ContextSnapshot, decision: RouterDecision, *,
                        publish_reply: bool) -> dict:
        if self._local_composer is None:
            return self._cloud_or_notice(
                text, snapshot, decision=decision, reason="chat_local_failed",
                publish_reply=publish_reply,
            )
        try:
            rendered = self._local_composer.generate(text, snapshot=snapshot, memory=self._memory)
        except Exception:
            logger.warning("local reply failed, falling back to cloud", exc_info=True)
            return self._cloud_or_notice(
                text, snapshot, decision=decision, reason="chat_local_failed",
                publish_reply=publish_reply,
            )
        if not rendered:
            return self._cloud_or_notice(
                text, snapshot, decision=decision, reason="chat_local_empty",
                publish_reply=publish_reply,
            )
        return self._result(
            rendered, True, reason="chat_local", route=decision.route,
            emotion=detect_emotion(text),
        )
```

- 删除：`_HubRouteDispatcher`、`_HubCloudFallback`、`_route_registry`、`register_route`、`_dispatch_chat_local`、`_dispatch_tool_local`、`_dispatch_vision`、`_is_allowed_tool_call`、`_render_tool_result`、`_snapshot_with_situation` 及 `DecisionRouter`/`RouteDispatcher` import（`_try_cloud` 已于 Task 2 被 `_run_cloud_loop` 取代）。
- `_cloud_or_notice` 签名改为 `(self, text, snapshot, *, decision=None, reason, publish_reply=True)`。成功的 cloud final 传 `emotion=detect_emotion(text)`；failed/空结果生成的 `L2_UNAVAILABLE_NOTICE` 固定 `Emotion.NEUTRAL`，不得继承用户输入情绪。transition/cancel 固定 neutral，危机固定 sadness。
- `_result` 去掉 `intent`/`trusted_metadata` 参数并保留可选 `reply_id`；`_handle_locked` 的默认 silent 分支去掉 `"intent"` 键；`DecisionTrace` 去掉 `intent` 字段但保留 Task 2 新增的 `reply_id`。
- `DecisionHub.__init__`/`build_brain`：删除 `sedimenter`、`vision_screen`、`local_tool_allowlist` 参数（`sedimenter` 已于 Task 3 删除）。
- `tests/cognition/test_hub.py`：删除 intent 断言（`records[0]["intent"]`、`intent=Intent.*` 构造、`FakeSedimenter` 残留）；本地路由用例改为 `GateRoute`。新增两条情绪边界测试：输入“气死我了”且 cloud 成功时 final 为 anger；同一输入在 cloud failed/空结果时 `L2_UNAVAILABLE_NOTICE` 为 neutral。

- [ ] **Step 7: `src/yuki/cognition/assembly.py` 与配置收尾**

- `_build_local_brain` 返回 `(router, composer)`：删除 `VisionScreenAdapter` 构造/注册与 `vision_screen` 传参；`LocalRouter(...)` 去掉 `registry=`/`local_tool_allowlist=`。
- `src/yuki/cognition/brain/local/__init__.py`：导出列表去掉 `VisionScreenAdapter`（`screen.py` 文件与 `test_local_screen.py` 保留，供未来本地工具复用）。
- `src/yuki/config.py`：`LocalBrainConfig` 删除 `local_tool_allowlist` 字段；`tests/test_config.py` 同步。
- 删除 `src/yuki/cognition/brain/route.py`、`tests/cognition/test_route.py`；用 `rg` 全仓确认无 `DecisionRouter|LocalRoute|VisionScreenAdapter|local_tool_allowlist` 残留（`screen.py`/`test_local_screen.py` 内定义除外）。

- [ ] **Step 8: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_classifier.py tests/cognition/test_local_router.py tests/cognition/test_hub.py tests/cognition/test_assembly.py tests/cognition/test_cognition.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: collapse L0 to binary local/cloud gate, remove Intent and 4-route dispatch"
```

---

### Task 5: 交互契约、文档与全仓回归

**Files:**
- Modify: `src/yuki/interaction/agent.py`、`src/yuki/interaction/tts_controller.py`、`src/yuki/payloads.py`、`src/yuki/bus_server/gateway.py`、`config.example.yaml`
- Modify: `tests/interaction/test_interaction.py`、`tests/interaction/test_tts_controller.py`、`tests/bus_server/test_gateway.py`、`tests/cognition/l2/test_bridge.py`（Task 2 引入多 system message 后的残留位置断言）
- 文档：本计划 + spec 自检。

**Interfaces:** `ReplyPayload.kind: NotRequired[Literal["transition", "final", "cancel"]]`、`reply_id: NotRequired[str]`；`TtsController(..., transition_grace_s=0.8)`、`speak(text, emotion="neutral", *, kind="final", reply_id=None)`、`cancel(reply_id)`；Gateway history 仅将 final/缺省 kind 作为正式 assistant turn。

- [ ] **Step 1: 更新 ReplyPayload 与 Interaction 分派**

`src/yuki/payloads.py` 先把 typing import 补为 `from typing import Literal, NotRequired, TypedDict`，再给 `ReplyPayload` 新增 kind/reply_id。`InteractionAgent` 对旧载荷缺省按 final，对 cancel 只调用取消接口：

构造默认 TTS 时传 `transition_grace_s=config.agent_loop.transition_grace_s`；注入的测试 TTS fake 同步升级 `speak/cancel` 协议。

```python
        def on_reply(topic: str, payload: dict) -> None:
            kind = payload.get("kind", "final")
            reply_id = payload.get("reply_id")
            if kind == "cancel":
                self._tts.cancel(reply_id)
                return
            self._tts.speak(
                payload["text"],
                emotion=payload.get("emotion", "neutral"),
                kind=kind,
                reply_id=reply_id,
            )
```

- [ ] **Step 2: `TtsController` 实现 kind-aware 调度（先测试）**

保持旧载荷 latest-final-wins 兼容，并增加以下状态规则：

1. transition 只在没有 active/pending final 时进入队列；不得中断 final。
2. 同 `reply_id` 的 final 到达时，先立即清除队列中的 pending transition。若 transition 正在合成但尚未 `_mark_speaking`，立即使其 generation/cancel token 失效；合成无法强杀时允许后台返回，但其结果必须丢弃，且不得进入 `player.play()`，不等待 grace。
3. 只有已经 `_mark_speaking` 的同 id transition 才适用 `transition_grace_s`：等待其自然结束至多 grace；超时后 `player.stop()`，再播放 final。
4. cancel 只清除/停止匹配非空 `reply_id` 的 transition；`reply_id=None` 必须 no-op，不得停止 final 或其他 reply_id。
5. 新 final 仍可替换旧 final，保持既有 latest-final-wins 语义。
6. `TTS_SPEAKING/TTS_FINISHED` 载荷补充 kind/reply_id，便于诊断；旧消费者可忽略新增字段。

新增确定性测试（用 Event/注入 clock，不用 `sleep` 猜时序）：

- pending transition → 同 id final：transition 立即从队列删除，final 不等待 grace；
- 正在合成但尚未 `_mark_speaking` 的 transition → 同 id final：解除合成 Event 后迟到结果被丢弃，transition 从未进入 `player.play()`；
- 已 `_mark_speaking` 的 transition → 同 id final：final 在 transition 自然完成或 grace 到期后播放；
- active final 时到达 transition：transition 被丢弃且不调用 `player.stop()`；
- cancel 匹配 transition：停止；cancel 不匹配或目标为 final：不停止；
- 无 kind/reply_id 的旧调用仍按 final 正常播放；
- Interaction 收到 cancel 不合成空文本。

- [ ] **Step 3: Gateway/Recorder 契约测试**

- Recorder 继续记录原始 REPLY（含 transition/cancel），供诊断回放。
- `GatewayRuntime.read_history` 遇到 `Topics.REPLY` 时，仅当 `payload.get("kind", "final") == "final"` 才追加 assistant turn。
- `tests/bus_server/test_gateway.py` 增加同一 reply_id 的 transition/cancel/final 记录，断言历史仅出现 final。

- [ ] **Step 4: `config.example.yaml` 最终同步**

- `cloud:` 节：`max_turns` 加注释 `# 弃用：由 agent_loop.max_steps 取代`。
- 新增：

```yaml
agent_loop:
  max_steps: 3            # None/缺省 → 回退 cloud.max_turns
  max_duration_s: 15.0
  transition_enabled: true
  transition_fallback: "让我看一下……"
  transition_grace_s: 0.8
  tool_result_max_chars: 2000
  compact_threshold_tokens: 0   # 0 = 关闭循环内压缩
  interrupt_enabled: true
```

- `persona:` 节加 `refresh_every_utterances: 30`；删除 `sedimenter:` 节；`local_brain:` 删除 `local_tool_allowlist`。
- 迁移提示：顶层 `sedimenter:` 会因 `Config.extra="forbid"` 启动失败，必须删除；当前嵌套 `LocalBrainConfig` 未设置 `extra="forbid"`，旧 `local_tool_allowlist` 会被忽略，但仍建议删除；`cloud.max_turns` 仍兼容。

- [ ] **Step 5: 更新 spec 的已知限制**

本迁移支持“模型思考/工具步骤期间，由完整 ASR utterance 触发中断”。当前 TTS 播放时 ASR 会暂停，因此真正的语音 barge-in 仍是后续工作：从 `Topics.MIC`/VAD 派生语音起始事件，先停止 TTS，再等待完整 ASR 文本。不得在本次验收中宣称已支持 TTS 播放期间抢话。

- [ ] **Step 6: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（e2e 默认跳过）。若失败，优先查 `rg "Intent|Sedimenter|LocalRoute|DecisionRouter|local_tool_allowlist|VisionScreenAdapter" src tests` 残留引用。

- [ ] **Step 7: e2e 回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -m e2e -q`
Expected: 全 PASS。另增加一条跨组件测试：Hub 发布 transition→cancel 时 Interaction 停止匹配 transition；transition→final 时 Gateway 历史只保留 final。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: add kind-aware transition TTS and final-only reply history"
```

---

## Self-Review 记录

- **评审覆盖**：① crisis tool_calls 由 `crisis_tool_calls_blocked` 硬拦截；② probe 读写均持锁，且只传给 `publish_reply=True` 的语音回合；③ deadline 后不发布结果；④ pending/合成中 transition 立即失效，仅 speaking transition 等 grace；⑤ 非敏感显式偏好只以 sensitivity=0 写入，敏感内容不落库；⑥ 非法参数不 dispatch；⑦ 成功 cloud final 检测 emotion，失败 notice 固定 neutral。
- **Spec 覆盖**：AgentLoop 的 deadline/crisis 工具守卫/公开偏好写入 → Task 1；reply_id/cancel、请求域中断与危机例外 → Task 2；Tuner/floor/不丢 periodic 触发 → Task 3；L0 local/cloud、非敏感显式偏好与 emotion → Task 4；REPLY typed payload、TTS 状态区分和 Gateway 历史 → Task 5。
- **一致性**：`AgentLoop` 在 Task 1 定义、Task 2 由 `CloudBridge`/Hub 消费；Bridge 摘要统一复用 `make_summarize`，无悬空常量；`GateRoute` 在 Task 4 替换 `LocalRoute`；`FLOOR_KEY` 先恢复再 clamp cooldown；`kind/reply_id` 由 Task 2 发布、Task 5 消费；`agent_loop.max_steps=None` 时回退 `cloud.max_turns`。
- **行为/兼容**：`CloudBridge.generate` 仍把 failed/interrupted/空文本转为 CloudError；旧 REPLY 缺省按 final；危机中断明确例外为发布安全兜底；旧 TunerState（无 `FLOOR_KEY`）加载安全。删除 Sedimenter 后不再做隐式多信号偏好推断，这是有意的行为收缩。非敏感显式偏好仍保留；敏感偏好不再通过 cloud 工具持久化。
- **已知限制**：同步工具无法安全强杀，只保证 deadline 后不再发布其结果或继续后续步骤；TTS 播放期间的 VAD barge-in 不在本迁移内，spec 不得宣称支持。
- **测试命令**：各任务内嵌 `& ".venv\Scripts\python.exe" -m pytest ...`；收尾全仓 + e2e。
