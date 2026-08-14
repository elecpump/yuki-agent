# Yuki L2 云桥设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：L2 云端深度回应通道——OpenAI 兼容客户端、工具调用多轮、L1/L2 层级选择、降级回退、记忆函数绑定

## 1. 背景与目标

实现设计文档 `2026-08-10-yuki-agent-design.md` §3.2/§4.3/§8.2 的 Cloud LLM Bridge：对复杂意图走云端深度回应（2~5s），与 L1 本地动作链并行，云端不可用时自动回退 L1。函数框架（`FunctionRegistry.dispatch/tool_schemas`）即为本桥的调用面；本轮同时把记忆函数绑定进注册表，让工具调用真正可用。

**已确认决策**：
- **OpenAI 兼容端点**：`base_url` 可配，覆盖 OpenAI/Azure/本地 ollama·vLLM 等兼容端点；工具调用格式与函数框架已对齐。
- **含工具调用 + 绑记忆函数**：L2 发 tool_calls → `FunctionRegistry.dispatch` → 结果回填云端（多轮）；本轮绑定 `memory.query/write/list/get`。
- **按 intent 标 tier**：`DecisionPolicy.tier_for(intent)` 独立方法决定 L1/L2，`decide` 签名不变。
- **静默等待**：L2 期间不发过渡 REPLY，等最终结果（TTS 尚为桩，流式/过渡留待交互层）。
- **L2 同步阻塞调用**：单用户、事件量低，先文档注明不做 worker 线程。

**范围外**：流式输出、"让我想想…"过渡、云端多模态（图/音频输入）、L2 决策链（分类/决策仍本地，仅文本生成本地不可及处走云）、跨进程 `functions/call` 服务。

## 2. 架构与文件布局

```
src/yuki/cognition/l2/
  __init__.py    — 导出 CloudBridge / CloudClient / CloudError / build_cloud_context
  client.py      — CloudClient：OpenAI 兼容 chat/completions（stdlib urllib，零新依赖）
  context.py     — build_cloud_context：情境摘要 + 敏感度过滤记忆 → 请求文本
  bridge.py      — CloudBridge：请求构建 + 工具调用多轮 + 降级
src/yuki/functions/
  memory_tools.py — register_memory_functions(registry, manager)：绑定 memory.query/write/list/get
```

- `client` 只做一次 HTTP；`bridge` 编排多轮工具调用；`context` 纯文本组装。各组件独立可测。

## 3. CloudClient（client.py）

```python
class CloudError(Exception): ...

class CloudClient:
    def __init__(self, base_url: str, model: str, api_key: str | None,
                 timeout_s: float = 10.0) -> None: ...
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...
```

- `chat` POST `{base_url}/chat/completions`（JSON，`Authorization: Bearer <api_key>`），返回解析后的 dict。
- 错误统一映射为 `CloudError`：网络异常 / 超时 / HTTP 非 2xx（含状态码）/ JSON 解析失败 / 缺 `choices`。
- 用 stdlib `urllib.request`，零新增依赖；换 httpx 为未来一行事。

## 4. 云端上下文（context.py）

```python
def build_cloud_context(utterance: str, situation: dict | None,
                        memory: MemoryManager | None) -> str: ...
```

- **纯文本**：utterance + 情境（`topic`/`summary`/`key_points`，不含帧/音频）+ 记忆检索 top-k（以 utterance 为查询，**过滤 `sensitivity == 2` 高敏**，满足 §5.3）。
- 无记忆/无情境时输出空段，不失败。

## 5. CloudBridge（bridge.py）

```python
class CloudBridge:
    def __init__(self, client: CloudClient, registry: FunctionRegistry | None = None,
                 system_prompt: str = DEFAULT_PERSONA_PROMPT, max_turns: int = 3) -> None: ...
    def generate(self, utterance: str, situation: dict | None,
                 memory: MemoryManager | None) -> str: ...
```

- `generate`：
  1. 组装 `messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": build_cloud_context(...)}]`。
  2. `tools = registry.tool_schemas()`（有 registry 时）。
  3. `client.chat(messages, tools)` → 若含 `tool_calls` → 逐个 `registry.dispatch(tool_call)` → 以 `{"role": "tool", "tool_call_id": ..., "content": json.dumps(result)}` 回填 → 再次 `client.chat`（循环至 `max_turns`）。
  4. 返回最终 assistant 文本（空 → 抛 `CloudError`）。
- 任何失败（网络/超时/工具循环超限）抛 `CloudError`，由 hub 降级到 L1。

## 6. 系统提示（persona）

- `DEFAULT_PERSONA_PROMPT`（代码常量）注入 `persona_name`：中文、简短、安全兜底优先、不替用户操作系统、可用工具查询记忆等。后续可配置化。

## 7. 层级选择（DecisionPolicy）

- `Tier` 枚举：`l1="l1"` / `l2="l2"`。
- `DecisionPolicy.tier_for(intent: Intent) -> Tier`（新方法，**不改 `decide` 签名**）。
- `L2_INTENTS = {ENTERTAINMENT, CREATIVE, ROLEPLAY, GAME, EMOTIONAL}`；其余（含 `SAFETY`，永远 L1）走 L1。
- L2 失败时的 fallback = `policy.decide(...)`（现有动作链）。

## 8. DecisionHub 集成

- `DecisionHub` 新增 `bridge: CloudBridge | None = None`（None → 恒 L1）。
- UTTERANCE 流程：classify → `tier = policy.tier_for(intent)` → **tier 为 L2 且 bridge 非 None** → `bridge.generate(utterance, situation, memory)`（**同步阻塞调用**，文档注明）→ 成功发布 REPLY；`CloudError` → 回退 `policy.decide` 动作链。**tier 为 L2 但 bridge 为 None（云端未配置）时直接走 L1 动作链**。L1 / awake / situation → 现有路径不变。
- `DecisionTrace` 增加 `tier` 字段（决策轨迹记录走 L1 还是 L2）。

## 9. 记忆函数绑定（memory_tools.py）

```python
def register_memory_functions(registry: FunctionRegistry, manager: MemoryManager) -> None: ...
```

绑定 4 个函数（pydantic 参数模型 → `tool_schemas` 自动导出）：

| 函数 | 参数 | 行为 |
|---|---|---|
| `memory.query` | `text: str`、`top_k: int = 5`、`type: str | None`、`min_sensitivity: int = 0` | `manager.query(...)` |
| `memory.write` | `memory_type`（枚举）、`content: str`、`confidence: float = 0.5`、`sensitivity: int = 0`、`source: str = "brain"`、`metadata: dict = {}` | `manager.write(...)` |
| `memory.list` | `type`、`min_sensitivity: int = 0` | `manager.list(...)` |
| `memory.get` | `id: int` | `manager.get(...)` |

- `CognitionAgent.setup` 在 registry 构建后调用 `register_memory_functions`。
- **隐私硬约束**：四个记忆函数的返回值统一过滤掉 `sensitivity == 2` 高敏条目（即使云端传入 `min_sensitivity=0`，也不得经工具获取高敏记忆），与 §4 上下文过滤一致。

## 10. 配置与隐私

```yaml
cloud:
  enabled: false              # 默认关，保持现有行为/e2e 不变；显式开启
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o-mini"
  api_key_env: "YUKI_CLOUD_API_KEY"   # 密钥仅环境变量，不落 config.yaml
  timeout_s: 10.0
  max_turns: 3
```

env：`YUKI_CLOUD_ENABLED` / `YUKI_CLOUD_BASE_URL` / `YUKI_CLOUD_MODEL` / `YUKI_CLOUD_API_KEY_ENV` / `YUKI_CLOUD_TIMEOUT_S` / `YUKI_CLOUD_MAX_TURNS`。

- 云端只收文本摘要，不含帧/音频；高敏记忆（`sensitivity == 2`）排除出云端检索；`api_key` 永不提交。

## 11. 健康与测试

- `_health_l2`：bridge 存在且 client 配置齐（`base_url`/`model` 非空）；不做心跳 ping。
- 测试：
  - `client`：注入假 HTTP 请求函数 → 请求体形状、`Bearer` 头、非 2xx/超时/解析失败 → `CloudError`。
  - `bridge`：注入假 client → 请求构建、工具调用多轮（tool_calls → dispatch → 回填 → 最终文本）、空响应 → `CloudError`、`max_turns` 超限。
  - `context`：记忆检索 + `sensitivity == 2` 被过滤。
  - `policy.tier_for`：L2_INTENTS → l2，其余 → l1，SAFETY → l1。
  - `hub`：L2 路由（假 bridge）、L2 失败 → L1 fallback、`tier` 进轨迹。
  - `memory_tools`：4 函数注册进 registry、`call` 可调。
- e2e 行为等价：cloud 默认关，`[yuki] 我在,你说。` 不变，REPLY 主题不变。

## 12. 风险与兼容

- 零协议变更：REPLY 主题与载荷不变；新增只在 cognition 内部 + functions 包。
- 零新依赖：`urllib.request` + 现有 pydantic。
- 默认关闭：不配 `YUKI_CLOUD_*` 时行为与现在完全一致。
- 已知限制：L2 同步阻塞调用线程；非流式；云端故障时仅回退 L1（无队列/重试策略，先 YAGNI）。
- **后续接入点**（明确范围外）：流式 + 过渡 REPLY、worker 线程、云端多模态、跨进程函数服务、persona 提示配置化。

## 13. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| OpenAI 兼容端点（base_url 可配） | 覆盖 OpenAI/Azure/本地 ollama·vLLM；工具格式已对齐 |
| 含工具调用 + 本轮绑记忆函数 | 函数框架就是为此建的；让 L2 真正能查/写记忆 |
| 按 intent 标 tier（独立 `tier_for`） | 不改 `decide` 签名，L1 路径零破坏 |
| 静默等待 | TTS 尚为桩；过渡/流式留交互层 |
| L2 同步阻塞 | 单用户事件量低；文档注明，不做线程（YAGNI） |
| `cloud.enabled` 默认 false | 保持现有行为/e2e；成本与隐私由用户显式开启 |
| api_key 走环境变量 | 密钥永不落库/提交 |
| 高敏记忆排除云端 | 设计 §5.3 隐私约束 |
