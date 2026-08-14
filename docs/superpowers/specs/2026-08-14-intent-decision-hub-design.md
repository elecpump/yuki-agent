# Yuki 意图识别与决策中枢设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：Brain 内核——意图识别 + 决策中枢，替换 L1Responder；含主动开口决策（固定冷却策略）

## 1. 背景与目标

实现设计文档 `2026-08-10-yuki-agent-design.md` §3.2/§4.1 的 Personality Brain 内核：把输入（用户话语 / 唤醒 / 阅读情境）转换为决策（回复文本 + 副动作），替换当前 `l1_responder.py` 的关键词直答（其 `# TODO(Brain)` 标注此即为落点）。

**已确认决策**：
- **完整决策链**：意图识别 + 决策中枢一次到位，替换 L1Responder。
- **规则分类器 + 接口占位**：`IntentClassifier`/`EmotionClassifier` 协议 + 规则实现，LLM 接入后可替换。
- **含主动开口决策**：本轮用固定冷却策略；反馈参数自调（环1）留待反馈闭环阶段。
- **决策产出**：回复文本（可组合原子动作）+ 记忆写入 + 函数调用 + 决策轨迹 + 层级选择（本轮恒 L1，L2 接口 TODO）。
- **动作可组合**：原子动作组合成复合回复（"共情 + 提问 + 轻松笑话"）。
- **安全危机独立兜底**：safety 意图短路正常决策链。
- **情绪与意图分离**：Emotion 为独立维度。

**范围外**：L2 云桥、反馈自进化闭环、真实 TTS、唤醒词、内容提供器（笑话/故事等仅内置少量 canned）。

## 2. 架构与文件布局

新增 `src/yuki/cognition/brain/` 包：

```
src/yuki/cognition/brain/
  __init__.py     — 导出 DecisionHub / Intent / Emotion / Action / DecisionPolicy / 分类器
  classifier.py   — Intent/Emotion 枚举 + IntentClassifier 协议 + RuleIntentClassifier + RuleEmotionClassifier
  actions.py      — Action dataclass + ActionExecutor 协议 + 内置执行器集合 ACTION_EXECUTORS
  policy.py       — TriggerKind 枚举 + DecisionPolicy（意图/触发 → 动作序列 + 主动开口冷却门控）
  hub.py          — DecisionHub（编排 + 渲染 + 决策轨迹）+ build_brain 装配函数
tests/
  test_classifier.py / test_policy.py / test_hub.py
删除: src/yuki/cognition/l1_responder.py、tests/cognition/test_l1_responder.py
保留: src/yuki/cognition/l1.py（L1Engine 作为 inform/chit_chat 动作的文本生成源）
```

- `classifier.py` 纯分类；`policy.py` 纯决策；`hub.py` 编排/渲染/轨迹。各组件独立可测。
- `CognitionAgent.setup()` 用 `build_brain(...)` 替换 `build_l1_responder`；订阅主题不变（AWAKE / USER_UTTERANCE / SITUATION_UPDATE）。

## 3. 分类器（classifier.py）

### 3.1 Intent 枚举（10 类）

| intent | 二级触发示例 |
|---|---|
| `chit_chat` 社交闲聊 | 你好 / 在吗 / 今天天气 / 你叫什么 |
| `emotional` 情感陪伴 | 难过 / 求安慰 / 升职了 / 好无聊 / 压力大 / 想你了 |
| `entertainment` 娱乐内容 | 讲个笑话 / 睡前故事 / 出个谜语 / 冷知识 / 推荐首歌 / 今天运势 |
| `game` 互动游戏 | 成语接龙 / 海龟汤 / 扮演侦探 / 猜数字 / 石头剪刀布 |
| `roleplay` 角色扮演 | 扮演我女朋友 / 你是哈利波特 |
| `creative` 创意创作 | 写首情诗 / 续写故事 / 帮我想网名 |
| `companion` 生活陪伴 | 今天吃火锅 / 陪我学习 / 睡不着 / 提醒喝水 / 圣诞快乐 |
| `system` 系统与反馈 | 你能做什么 / 温柔一点 / 回答得不好 / 再见 |
| `safety` 安全危机 | 自伤 / 自杀 / 想死（**优先匹配，短路正常链**） |
| `unknown` 其他/未知 | 乱输入 / 多意图混合（兜底） |

### 3.2 Emotion 枚举（独立维度）

`neutral / joy / sadness / anxiety / anger / love / tired`（关键词启发式，未命中 → neutral）。

### 3.3 接口与规则实现

```python
class IntentClassifier(Protocol):
    def classify(self, text: str) -> Intent: ...

class RuleIntentClassifier:
    def __init__(self, rules: dict[str, Intent] | None = None) -> None: ...  # 关键词 → intent
    def classify(self, text: str) -> Intent: ...  # safety 优先，再按规则表顺序，未命中 unknown

class RuleEmotionClassifier:
    def __init__(self, rules: dict[str, Emotion] | None = None) -> None: ...
    def classify(self, text: str) -> Emotion: ...
```

- 规则表数据驱动（dict 可注入，测试用）；关键词匹配为子串匹配（文本已小写化）。
- **safety 规则在 `RuleIntentClassifier` 内最先检查**。

## 4. 原子动作空间（actions.py）

```python
@dataclass(frozen=True)
class Action:
    name: str
    params: dict = field(default_factory=dict)

class ActionExecutor(Protocol):
    def __call__(self, action: Action, ctx: ActionContext) -> str: ...
```

`ActionContext` 携带执行环境：`{intent, emotion, situation, memory, registry}`。每个执行器返回文本片段，可带副动作。

| 动作 | 行为 |
|---|---|
| `empathize` | 共情（按 emotion 参数化模板） |
| `acknowledge` | 附和/确认 |
| `comfort` | 安慰 |
| `encourage` | 鼓励 |
| `ask` | 提问（注入情境 topic） |
| `clarify` | 澄清（unknown 时） |
| `inform` | 告知（chit_chat/system 委托 L1Engine 生成） |
| `joke` | 笑话（内置少量 canned） |
| `story` | 故事（内置少量 canned） |
| `invite_game` | 邀请游戏 |
| `farewell` | 告别 |
| `safety_escalate` | 安全兜底：关怀 + 求助指引（不调 L2） |
| `write_memory` | 副动作：经注入 `MemoryManager` 直连写入。参数约定 `memory_type="scenario"`（披露类写 `preference`）、`content=<话语文本>`、`source="brain"`、`metadata={}` |
| `call_function` | 副动作：经注入 `FunctionRegistry` 调用（暂无业务函数，结构就位） |
| `stay_silent` | 不开口（主动开口门控未过） |

- 无内置内容（joke/story 池为空）时返回占位"这个我还在学…"。
- 动作名全集即上述 15 个；`ACTION_EXECUTORS` 提供默认实现，测试可注入自定义执行器。

## 5. 决策策略（policy.py）

```python
class TriggerKind(Enum):
    UTTERANCE = "utterance"
    AWAKE = "awake"
    SITUATION = "situation"

class DecisionPolicy:
    def __init__(self, proactive_cooldown_s: float, policy_table: dict | None = None) -> None: ...
    def decide(self, trigger: TriggerKind, intent: Intent, emotion: Emotion,
               text: str, situation: dict | None,
               last_open_ts: float | None, now: float) -> list[Action]: ...
```

- `UTTERANCE`：按 `{intent: [actions]}` 策略表选动作；`safety` → `[safety_escalate]`（短路，跳过一切副动作）；`unknown` → `[clarify]`。
- `system` 内部分流：`text` 命中告别关键词（`再见/晚安/下次聊/拜拜`）→ `[farewell]`；否则（功能询问/偏好/反馈）→ `[inform]`。
- `emotional` → `[empathize, ask]`；`entertainment` → `[joke]`（或 `[story]`，按策略表）；`game` → `[invite_game]`；其余默认 → `[inform]`。
- `AWAKE`：→ `[inform]`（L1 生成 `我在，你说。`，保持 e2e 断言）。
- `SITUATION`（主动开口）：`proactive_enabled` 关闭时恒 `[stay_silent]`；否则冷却门控 `now - last_open_ts >= proactive_cooldown_s` 且情境非敏感且 topic 非空 → 主动动作（如 `[acknowledge, ask]`），任一条件不满足 → `[stay_silent]`。
- 冷却状态由 hub 维护（距上次"开口"的 `last_open_ts`）；本轮固定策略，无参数自调。

## 6. 决策中枢（hub.py）

```python
class DecisionHub:
    def __init__(self, bus, *, intent_clf, emotion_clf, policy,
                 memory, registry, cooldown_s: float) -> None: ...
    def on_awake(self, topic, payload) -> None: ...
    def on_user_utterance(self, topic, payload) -> None: ...
    def on_situation_update(self, topic, payload) -> None: ...

def build_brain(bus, *, memory=None, registry=None, config=None) -> DecisionHub: ...
```

- 订阅 `AWAKE / USER_UTTERANCE / SITUATION_UPDATE`（与 L1Responder 相同）。
- 流程：`classify(text) → 注入 situation/memory → policy.decide → 执行动作（渲染文本 + 副动作） → publish REPLY + 记决策轨迹`。
- `write_memory` 副动作：直接调 `MemoryManager.write`（同进程，cognition 持有记忆）；参数按 §4 动作表约定（`memory_type`/`content`/`source="brain"`）。
- `call_function` 副动作：调 `FunctionRegistry.dispatch/call`（本阶段无业务函数，仅结构化就位）。
- **决策轨迹** `DecisionTrace`：`{ts, trigger, intent, emotion, actions, rendered, reason, cooldown_state}`，经决策 logger 落盘（复用 `logger.get_decision_logger()`）。
- `CognitionAgent`：`health_components()` 的 `l1` 项替换为 `brain`（hub 构造成功即 ok）。

## 7. 配置

`Config` 新增 `brain:` 节（env `YUKI_BRAIN_*`）：

```yaml
brain:
  proactive_cooldown_s: 120.0
  proactive_enabled: true
```

- `proactive_enabled: false` 时，SITUATION 触发恒 `[stay_silent]`。
- 关键词规则表本轮放代码常量（可注入），不做配置。

## 8. 测试与兼容

- `test_classifier.py`：各 intent 关键词命中；safety 优先于其他；unknown 兜底；情绪识别；空串 → unknown/neutral。
- `test_policy.py`：意图→动作映射；safety 短路；unknown→clarify；system→farewell；冷却门控（通过/未过/禁用）。
- `test_hub.py`：utterance→REPLY 文本；emotional→共情+提问；披露→write_memory 副动作（memory 被写入）；unknown→澄清；safety→升级且不写正常链；冷却后主动开口 / 冷却内 stay_silent；awake→`我在，你说。`；决策轨迹落盘。
- **e2e 行为等价**：awake 回复 `我在，你说。`；REPLY 主题不变；`CognitionAgent` 订阅断言（AWAKE/SITUATION_UPDATE/USER_UTTERANCE）不变。
- 全仓回归：删除 l1_responder 相关测试，新增 brain 测试；`python -m pytest` 全绿。

## 9. 风险与兼容

- 零协议变更：REPLY 主题与载荷不变；新增只发生在 cognition 内部。
- 删除 `l1_responder.py`/`test_l1_responder.py`：其订阅与产出由 DecisionHub 承接，无外部引用（`build_l1_responder` 仅被 `cognition/agent.py` 使用）。
- 零新依赖：仅 stdlib + 现有 pydantic。
- 已知限制：规则分类器对多意图/长句精度有限——unknown 兜底 + 澄清动作缓解；LLM 接入时替换 classifier 实现即可。
- **后续接入点**（明确范围外）：L2 层级选择、反馈环1参数自调、内容提供器、唤醒词→UTTERANCE 直连。

## 10. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 规则分类器 + 协议接口 | 零模型、可测、与 L1 一致；LLM 接入替换实现 |
| 意图/情绪分离 | 设计建议；情绪是独立维度 |
| safety 短路正常链 | 安全兜底不可被其他意图规则淹没 |
| 原子动作可组合 | 用户明确要求；策略表天然表达复合回复 |
| 主动开口固定冷却 | 本轮不做参数自调（反馈环1留待闭环阶段） |
| 记忆直连 MemoryManager、函数经 FunctionRegistry | 同进程直连最简；函数框架留给 LLM 面与未来业务函数 |
| brain 位于 cognition 下 | 决策属认知层，与设计文档一致 |
