# 主动开口重构设计（修订稿）

> 日期：2026-08-26
> 状态：已实施（2026-08-28）
> 范围：cognition 主动开口——混合门控 + LLM 决策 + 动态冷却；替换 `DecisionPolicy` / `FeedbackTuner` / `TunerSink` / 情境模板拼接

## 1. 背景：首版设计的评审结论

首版设计（LLM 全权决策 speak/silent + 频率表冷却）方向正确，但有 8 个问题，本修订稿逐一解决：

| # | 首版问题 | 本稿落点 |
|---|---|---|
| 1 | LLM 调用阻塞 `_decision_lock`，拖慢用户话语处理 | §7 异步 worker + 发布前复查探针 |
| 2 | 每次情境变化 × 冷却到期都调 LLM，成本失控 | §4 免费硬门（防抖/去重/活跃抑制/冷却） |
| 3 | 只有 SITUATION_UPDATE 入口，破冰档（60s）永不生效 | §3 新增时间 tick 入口 |
| 4 | 频率表混用维度、边界未定义、silent ×1.5 无上限、无持久化 | §5 双轴分离 + 上限 + 落盘 |
| 5 | 丢掉"太吵/话多"显式负反馈信号（旧 Tuner 极性检测 + floor） | §5.4 保留极性检测与持久化 floor |
| 6 | ProactiveAgent 与 L0/L1 接口、prompt 结构、静默先验未定义 | §6 |
| 7 | binding core values 从代码硬阻断降级为 prompt 软约束，未声明 | §6.3 / §14 |
| 8 | 格式错误 ×1.5 把"解析噪声"当"情境信号"，语义混淆 | §8 错误分轨：backoff 与 signal 分离 |

核心原则：**LLM 只负责"值不值得说 + 说什么"；抑制、去重、频率全部用免费规则硬门在前**。这既控制成本与延迟，也规避 chat 模型"有问必答"导致的过度搭话。

## 2. 架构总览（混合门控）

```
SITUATION_UPDATE ─┐
                  ├─→ Hub 硬门（免费规则，任一不过 → 沉默，不调 LLM）
定时 tick（破冰）─┘        G1 开关 → G2 防抖/尊重silent → G3 活跃抑制 → G4 冷却到期
                                    ↓ 全过
                          ProactiveAgent（异步 worker，云端 LLM）
                                    ↓
                          speak → 复查探针 → 发布 REPLY + 写回 context + 更新冷却
                          silent / 失败 → 更新冷却（分轨）→ 记决策日志
```

新增组件：

- `CooldownCalculator`（`cognition/brain/cooldown.py`）：频率档位 + silent 乘数 + 负反馈 floor + 持久化。
- `ProactiveAgent`（`cognition/l2/proactive.py`）：云端 LLM 决策 speak/silent + 生成文本。

## 3. 触发入口

两个入口，同一评估路径：

1. **SITUATION_UPDATE**（保留）：情境变化 → 更新 context → 过硬门。
2. **定时 tick**（新增，破冰专用）：`CognitionAgent` 启动 `threading.Timer` 循环，间隔 `proactive_tick_s`（默认 30s）。tick 仅做评估（过硬门 + 调 LLM），不强制说话——解决"用户静默且屏幕静止时无更新、60s 破冰档永不生效"的问题。

tick 与情境更新共用同一把评估函数与 worker，天然串行（§7）。

## 4. 硬门（Hub 内免费规则，任一不过即沉默）

按顺序短路，全部免费，不触碰 LLM：

| 门 | 规则 | 说明 |
|---|---|---|
| G1 | `proactive_enabled == false` → 沉默 | 配置总开关（保留 `config.brain.proactive_enabled`） |
| G2a | 防抖：同 fingerprint 且距上次决策 < `dedup_min_interval_s`（30s）→ 沉默 | fingerprint = `(source_id, topic)`；滚动（scroll_band 变化）、焦点抖动产生的高频 SITUATION_UPDATE 在此被吞掉 |
| G2b | 尊重 silent：同 fingerprint 且上次决策为 silent 且 < `silent_hold_s`（300s）→ 沉默 | 模型说过"不值得说"后，同一情境 5 分钟内不再问第二次 |
| G3 | 活跃抑制：距上次用户 utterance < `activity_suppress_s`（30s）→ 沉默 | 用户正在对话/打字时绝不搭话；同时避免 proactive final 抢占对话 transition 的 TTS 调度 |
| G4 | 冷却：`now < next_available_ts` → 沉默 | 由 CooldownCalculator 维护（§5） |

判定顺序即上表顺序。G3 需要 hub 记录 `last_utterance_ts`（新增；`on_user_utterance_probe` 已有时间戳，可顺带维护）。

## 5. CooldownCalculator（`cognition/brain/cooldown.py`）

### 5.1 双轴模型（解决首版维度混淆）

两个独立维度，**先判静默、再判频率**：

```
A. 静默破冰轴：距上次 utterance > 600s（10min）→ base = 60s
B. 交互频率轴（5min 滑动窗口内互动次数）：
   ≥ 4 次 → base = 300s（不打断对话流）
   1-3 次 → base = 120s（正常节奏）
   0 次（但 < 10min）→ base = 120s（默认，覆盖冷启动）
```

边界完备：任意时刻必命中且只命中一档（A 优先于 B；A 命中时 B 窗口内必为 0 次，无歧义）。互动统计口径：语音 utterance + 桌面 chat（`handle_chat_request`）都计入；窗口为滑动窗口，`on_user_utterance(ts)` 追加。

### 5.2 决策后冷却更新（`next_available_ts` 语义）

冷却语义从"距上次开口"改为"距上次决策尝试"，由 `CooldownCalculator` 维护绝对时间戳 `next_available_ts`：

| 决策 | 更新 | 说明 |
|---|---|---|
| speak | `next = now + base`，`silent_streak = 0` | 正常节奏 |
| silent | `next = now + min(base × 1.5^silent_streak, max_cooldown_s)`，`silent_streak += 1`（封顶 3 次复合） | 模型说"不值得说"→ 后退；上限 600s，避免与破冰档打架 |
| LLM 失败 | `next = now + min(base × 2, max_cooldown_s)` | backoff，不叠加 silent_streak |
| 格式错误 | `next = now + min(base × 1.5, max_cooldown_s)` | 纯 backoff（噪声，非信号），不叠加 silent_streak |

`silent_streak` 仅 speak 清零；LLM 成功调用（无论 speak/silent）清零失败计数。

### 5.3 持久化

写入 `data/cooldown_state.json`（替代 `tuner_state.json`）：

```json
{ "persona_name": "yuki", "cooldown_s": 120.0, "floor_s": 30.0,
  "silent_streak": 0, "updated_at": "2026-08-26T12:00:00" }
```

启动时恢复；`persona_name` 不匹配则忽略（与 `TunerStateStore` 现行为一致）。

### 5.4 显式负反馈（保留旧 Tuner 的极性机制）

复用 `tuner.py` 的 `detect_polarity`（迁移进 `cooldown.py`）：

- 用户 utterance 命中负向词（"太吵/话多/闭嘴/安静…"）→ `cooldown = max(cooldown × 1.5, floor + floor_step_s)`；连续 3 次负反馈 → `floor += 30s`（上限 600s）并持久化。
- 正向词（"说得好/继续…"）→ `cooldown × 0.8`，`floor` 不变。

理由：负反馈是**代码级信号**，不能依赖 LLM 从历史里自行领悟；且"太吵"本身算一次互动，若只走频率轴会把用户推向"频繁互动 → 300s"档，方向恰好相反。

### 5.5 接口

```python
class CooldownCalculator:
    def __init__(self, initial_s: float = 120.0, *, path=None, persona_name="yuki",
                 window_s=300.0, max_cooldown_s=600.0, floor_step_s=30.0) -> None: ...
    def on_user_utterance(self, text: str, ts: float) -> None: ...   # 频率窗口 + 极性
    def base_cooldown(self, now: float) -> float: ...                # §5.1 档位
    def on_decision(self, outcome: str, now: float) -> None: ...     # speak|silent|fail|parse_error
    def is_available(self, now: float) -> bool: ...                  # now >= next_available_ts
    def last_utterance_ts(self) -> float | None: ...
```

`base_cooldown()` 始终返回 §5.1 的固定活动档位。持久化的 `cooldown_s` 以 120s 为
基准形成独立反馈倍率，只在计算 `next_available_ts` 时作用于档位值；因此频率分类与
显式正负反馈不会互相覆盖。

## 6. ProactiveAgent（`cognition/l2/proactive.py`）

### 6.1 接口

```python
@dataclass(frozen=True)
class ProactiveDecision:
    action: str        # "speak" | "silent"
    text: str          # speak 时的回复文本
    reason: str        # 模型给出的决策原因（记日志）
    raw: str | None    # 原始模型输出（调试）

class ProactiveAgent:
    def __init__(self, client: CloudClient, *, system_prompt: str | None = None,
                 view_builder: CloudViewBuilder | None = None,
                 timeout_s: float = 5.0, max_chars: int = 200) -> None: ...
    def decide(self, snapshot: ContextSnapshot, soul: dict | None = None) -> ProactiveDecision: ...
```

- 复用 `CloudClient`（与对话路径同一云端配置）；**不注入 tools、不写记忆**——主动开口是单向输出，情境内容不构成偏好。
- 输出 JSON（要求模型只输出 JSON）：

```json
{"action": "speak" | "silent", "text": "……（speak 时必须非空）", "reason": "……"}
```

解析：剥离代码块 → `json.loads` → 校验 `action` 枚举与 `text` 非空。失败 → `ProactiveDecision("silent", reason="parse_error")`（hub 记 parse 失败计数，走 §8 分轨）。

### 6.2 Prompt 结构

- **system**：人格描述（`soul.personality_description`）+ 特质语义化（5 维 traits 转文字）+ **binding core values 原文列表（软约束）** + 主动开口行为准则：

```
【主动开口准则】
- 只有对用户当前正在看/做的事有真正值得说的内容时才 speak。
- speak 时 1-2 句，自然口语，像朋友随口搭话；不评价用户；不转述屏幕内容本身。
- 没有值得说的就 silent。silent 是完全正常且受鼓励的输出，不要找话说。
- 用户可能正在专注工作或阅读，不确定时倾向 silent。
- 只输出 JSON，不要输出其他文字。
```

- **user**：`CloudViewBuilder` 视图（复用 `format`，utterance 传空串）→ 情境（topic/summary/key_points）+ 最近对话轮 + 折叠摘要。token 预算沿用 view builder 的 `max_tokens`，无需新机制。
- **静默先验**：few-shot 2 例（一例 speak、一例 silent，silent 例占比不低），温度 0.5，`max_tokens` 100。chat 模型被训练成"有问必答"，此先验是防止退化成"永远搭话"的关键，不可省略。

### 6.3 人格注入与约束降级声明

- `Soul` 保留；`personality_traits` 现状已是静态（`FeedbackTuner` 只调 cooldown，不调 traits），本稿不改变这一点，仅需把 traits 语义化注入 prompt。
- **明确声明**：binding core values 从 `DecisionPolicy` 的代码级 `blocks` 硬阻断（`policy.py:39-47`）降级为 prompt 软约束。当前 `cv.safety`/`cv.companionship` 的 `blocks` 均为空，风险低；若未来新增 binding block，需在 hub 增加代码级回复过滤钩子（§14）。

## 7. 并发与发布协议

- **异步 worker**：硬门通过后，hub 把评估提交到独立 worker 线程（复用 `_run_periodic` 的 worker 模式，`hub.py:273-293`），**绝不占用 `_decision_lock` 执行 LLM 调用**——用户话语处理不被主动开口阻塞。
- **发布前复查**：speak 结果发布前检查探针（复用 `_pending_input_ts`，`hub.py:487-489`）：若 `pending_input_ts > 决策开始时间` → 放弃发布，按 silent 处理（不更新 silent_streak，只更新 `next_available_ts = now + base` 防止立即重试）。
- **上下文写回**：speak 发布成功后 `context.add_agent(text)`（保留 `hub.py:237-238` 现行为），proactive 回合进入对话历史，后续 LLM 上下文连续。
- **REPLY 契约不变**：`{text, ts, emotion: "neutral", kind: "final", reply_id}`；新 reply_id，与对话回合互不干扰。
- **TTS 冲突**：由 G3 活跃抑制兜底（用户 30s 内有话语 → 不触发），无需 TTS 调度改动。

## 8. 错误处理（backoff 与 signal 分轨）

| 场景 | 处理 | 性质 |
|---|---|---|
| 云端 LLM 调用失败（网络/超时/HTTP） | 沉默，`next = now + min(base×2, 600)`，`failure_streak += 1`，记错误 | backoff（噪声） |
| LLM 返回格式错误 | 沉默，`next = now + min(base×1.5, 600)`，`failure_streak += 1`，记警告 | backoff（噪声，不叠加 silent_streak） |
| 连续失败 3 次（`failure_streak >= 3`） | 禁用主动开口 60s（`disabled_until = now + 60`），hard 门 G1 后加一道运行时禁用检查；任何成功调用清零 `failure_streak` | 熔断 |

状态机归属：`failure_streak` / `disabled_until` 由 hub 持有（决策编排责任），`CooldownCalculator` 只认 outcome。云端不可用时主动开口静默即可，**不发** L2 不可用 notice（与对话路径的 `L2_UNAVAILABLE_NOTICE` 不同——主动开口不该打扰）。

## 9. 数据流

```
SITUATION_UPDATE / 定时 tick
  → Hub 评估（免费）：
      G1 proactive_enabled？
      G2a 防抖（fingerprint 30s 内）？
      G2b 尊重 silent（同情境 silent_hold_s 内）？
      G3 用户 30s 内有 utterance？
      G4 next_available_ts 到期？
      任一否 → 沉默（仅记 trace，不调 LLM）
  → 提交 worker：
      snapshot = projector.build(context_wrapper) + soul
      ProactiveAgent.decide(snapshot, soul)
        → speak：复查探针 → 发布 REPLY(final) → context.add_agent → cooldown.on_decision("speak")
        → silent：cooldown.on_decision("silent")
        → 失败/格式错：cooldown.on_decision("fail"/"parse_error")，failure_streak 计数，熔断检查
  → DecisionTrace 落盘（trigger/action/reason/cooldown_state 含 base·streak·next_available_ts）
```

## 10. 组件变更与文件布局

**移除**：

| 组件 | 原因 | 注意 |
|---|---|---|
| `brain/policy.py`（`DecisionPolicy`/`SituationAction`） | 被硬门 + ProactiveAgent 替代 | `TriggerKind` 枚举仍被 trace 使用，移入 `hub.py` 或 `classifier.py` |
| `brain/tuner.py`（`FeedbackTuner`） | cooldown 由 `CooldownCalculator` 接管 | `detect_polarity` 与负向/正向词表迁移至 `cooldown.py` |
| `brain/sink.py`（`TunerSink`/`DecisionSink`） | 无 tuner 即无 sink | hub 的 `register_sink` 一并删除 |
| `hub._render_situation_actions()` / `_handle_situation()` | 模板拼接被 LLM 生成替代 | |

**保留**：`SoulStore`（traits 静态化已成立，仅注入方式变化）；`WorkingContext`/`ContextProjector`（speak 写回 agent turn）；`CloudViewBuilder`（proactive prompt 复用）；`REPLY` 契约。

**新增**：

```
src/yuki/cognition/brain/cooldown.py   — CooldownCalculator + detect_polarity
src/yuki/cognition/brain/proactive_controller.py — Hub 持有的硬门、worker 与 tick 编排
src/yuki/cognition/l2/proactive.py     — ProactiveAgent + ProactiveDecision
```

`DecisionHub.__init__` 参数变更：删 `policy`/`tuner`；增 `proactive_agent`/`cooldown_calculator`/`proactive_tick_s`。`build_brain` 同步调整装配；破冰 tick 由 `CognitionAgent`/`build_brain` 启动。

## 11. 配置（`config.py` `BrainConfig` 扩展）

```yaml
brain:
  proactive_enabled: true        # 总开关（保留）
  proactive_cooldown_s: 120.0    # 初始冷却（保留，作 CooldownCalculator 初值）
  proactive_timeout_s: 5.0       # LLM 决策调用超时
  proactive_tick_s: 30.0         # 破冰定时器间隔
  proactive_max_chars: 200       # speak 文本上限
  activity_suppress_s: 30.0      # G3 活跃抑制窗口
  dedup_min_interval_s: 30.0     # G2a 防抖窗口
  silent_hold_s: 300.0           # G2b 尊重 silent 窗口
  max_cooldown_s: 600.0          # 冷却上限（CooldownCalculator）
```

`SoulConfig.tuner_state_path` → 改名 `cooldown_state_path`（默认 `data/cooldown_state.json`）；迁移逻辑见 §12。

## 12. 兼容与迁移

- **零协议变更**：REPLY 载荷、订阅主题（AWAKE/UTTERANCE/SITUATION_UPDATE）不变；proactive 输出为普通 final reply。
- **tuner_state 迁移**：`CooldownCalculator` 启动时若存在旧 `tuner_state.json` 且无新文件，读取一次 `proactive_cooldown_s`/`cooldown_floor_s` 写入新文件，随后旧文件不再读写（`SoulStore._migrate_legacy_params` 中 `COOLDOWN_KEY` 分支同步改为写新文件或删除）。
- **配置兼容**：旧 `brain.proactive_cooldown_s` 语义保留（初值）；新增字段全部带默认值，旧 `config.yaml` 可直接用。
- **e2e 行为等价**：`CognitionAgent` 订阅断言、REPLY 主题不变；主动开口在 LLM 不可用时退化为全沉默（旧行为等价于 `proactive_enabled=false` 的可观测差异仅在出现 speak 时）。

## 13. 测试计划

- `tests/cognition/test_cooldown.py`：档位边界（0/1/3/4 次互动、>10min 破冰、先静默后频率的优先级）；silent 复合上限；负反馈 floor 提升与持久化；`cooldown_state.json` 恢复；persona 不匹配忽略。
- `tests/cognition/l2/test_proactive.py`：JSON 解析（合法/代码块包裹/非法 JSON/缺字段/空 text）；speak 文本截断；mock `CloudClient` 抛 `CloudError` → `ProactiveDecision` 失败语义；few-shot prompt 含 silent 先验断言（注入 fake client 抓 messages）。
- `tests/cognition/test_hub_proactive.py`：硬门矩阵（G1 关、G2a 防抖、G2b silent hold、G3 活跃抑制、G4 冷却）；worker 发布前探针复查（决策期间来 utterance → 不发布）；speak 写回 context（`add_agent` 被调）；trace 落盘含 `llm_reason`；连续 3 失败 → 熔断 60s → 恢复；tick 定时触发（注入短 `proactive_tick_s` + fake clock）。
- 全仓回归：删 `test_policy.py`/tuner 相关测试，`pytest` 全绿；e2e 断言不变。

## 14. 风险与已知限制

| 风险 | 缓解 |
|---|---|
| 静默先验校准失败（模型过度/不足搭话） | few-shot + 温度 0.5 + 发布率监控（trace 里 action 分布）；发布率异常时先调 prompt 再调温度 |
| binding core values 软约束化 | 当前 blocks 为空、风险低；未来新增 binding block 时在 hub 加代码级回复过滤钩子 |
| LLM 调用成本 | 硬门保证正常使用 1 次/2-5min 量级；`max_tokens=100`、短超时；破冰场景受冷却 60s 限制 |
| 云端不可用期间行为 | 静默 + backoff，不发 notice，不打扰 |
| 同步 worker 无法强杀 | 超时（5s）+ 发布前探针复查保证过期结果不发布（同 L1 loop 的"过期结果不进入后续步骤"原则） |

## 15. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 混合门控：免费硬门在前，LLM 只管决策与内容 | 成本/延迟可控；静默先验难校准，规则抑制更可靠 |
| 硬门四道：开关/防抖/活跃抑制/冷却 | 覆盖成本、打扰、TTS 冲突三类问题 |
| 破冰定时 tick 独立于 SITUATION_UPDATE | 屏幕静止时破冰档才能生效 |
| Cooldown 双轴：静默轴优先于频率轴，边界完备 | 消除首版维度混淆与档位歧义 |
| silent 复合乘数封顶 600s | 避免与破冰档自相矛盾 |
| 保留极性负反馈 + 持久化 floor | "太吵"是代码级信号；频率轴对负反馈方向相反 |
| 异步 worker + 发布前探针复查 | 主动开口不阻塞、不抢占用户回合 |
| 格式错误按 backoff 处理，与 silent 信号分轨 | 解析故障是噪声，不应惩罚未来尝试 |
| ProactiveAgent 无 tools、不写记忆 | 单向输出；情境内容不构成偏好 |
| binding 约束软约束化并显式声明 | 当前 blocks 为空，可接受；预留代码级兜底钩子 |
