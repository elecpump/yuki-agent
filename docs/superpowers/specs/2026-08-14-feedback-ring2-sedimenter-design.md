# Yuki 反馈闭环环2: 偏好沉淀设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：环2 偏好沉淀——重复反馈模式 → 偏好记忆（带置信度），可显式纠正；消费：沉淀即记忆 + tuner 冷却偏置

## 1. 背景与目标

实现设计文档 `2026-08-10-yuki-agent-design.md` §6.3 的**环2 偏好沉淀**（中期）：把重复出现的反馈模式沉淀为带置信度的偏好记忆（可显式纠正），并回馈给环1 的冷却调参。沉淀结果即记忆（MemoryManager preference 类型），天然持久化、天然被 L2 云端上下文/工具消费。

**已确认决策**：
- **三维度**：交互节奏（开口频率/篇幅，来自极性反馈）、显式陈述（用户直接说）、话题兴趣（简化：接话记录情境 topic，同 topic 达阈值沉淀）。
- **计数置信度 + 阈值**：每 label 维护 hits/contradicts 计数；置信度 = `hits/(hits+1)`；`hits >= min_signals` 且 `hits/(hits+contradicts) >= confidence_threshold` 才沉淀。
- **话语 + 工具纠偏**：话语纠正（检测纠正词）写显式偏好并删冲突隐式偏好；现有 memory 工具（delete/strengthen）保留。
- **消费**：沉淀即记忆（L2 已读）；`FeedbackTuner.set_cooldown_floor` 冷却偏置。
- **`detect_polarity` 从 tuner 抽出共享**，tuner 与 sedimenter 共用。

**范围外**：环3 人格快照、打断/跳页信号、话题级更精细的兴趣建模（本轮回 string topic 简化版）、偏好记忆的 SQLite 持久化迁移（已在 MemoryManager）。

## 2. 架构与文件布局

```
src/yuki/cognition/brain/
  sedimenter.py   — PreferenceSedimenter（维度计数/置信度/沉淀/显式纠正）
  tuner.py        — 抽 detect_polarity 共享函数；增 set_cooldown_floor(value)
src/yuki/config.py — sedimenter: 节
tests/cognition/test_sedimenter.py / test_tuner.py（增）/ test_hub.py（增）
```

- `PreferenceSedimenter(memory, *, min_signals=3, confidence_threshold=0.6, topic_engagement_threshold=3)`，hub 在同一信号点喂入（与 tuner 并列），两者解耦、共用极性关键词。

**信号 API**（hub 在 `_handle` 调）：
- `on_user_utterance(text, intent)`：做 `detect_polarity(text)` 判极性（节奏维度）；`intent == SYSTEM` 时检测显式陈述/纠正词（显式维度）。
- `on_engagement(topic)`：每次 UTTERANCE 且 `effective_situation.topic` 非空时，hub 把 topic 传入（简化：在阅读某话题时与 agent 互动 = 对该话题的兴趣信号）。

## 3. 维度与信号

| 维度 | 信号 | 沉淀（content + metadata.label） |
|---|---|---|
| `interaction_rhythm` | `on_user_utterance` 的极性：负 → "用户不喜欢频繁主动开口"；正 → "用户喜欢主动互动"；负且含"话多/啰嗦" → "用户希望回复更简短" | `yuki.rhythm.frequency` / `yuki.rhythm.length` |
| `explicit` | `on_user_utterance` 且 intent=SYSTEM + 陈述关键词（"我喜欢/我不喜欢/请/别/不要…"）→ 沉淀原句 | 原句（metadata.source="user", confidence=1.0） |
| `topic_interest`（简化） | `on_engagement(topic)`：同 topic 接话 ≥ 阈值 → "对{topic}话题感兴趣" | `yuki.topic.{topic}`（metadata.source="feedback"） |

- 极性由共享 `detect_polarity(text)` 判定（`tuner.detect_polarity`，负/正/中性三值）。
- 显式陈述与显式纠正：陈述 → 沉淀；纠正词（"其实我不…""说反了""我改主意了"）→ 覆盖冲突隐式偏好。

## 4. 置信度模型（计数 + 阈值）

- 每 label 维护 `{hits, contradicts}` 计数器（进程内）；置信度 = `hits/(hits+1)`（首击 0.5，渐近 1）。
- **沉淀条件**：`hits >= min_signals` 且 `hits/(hits+contradicts) >= confidence_threshold`。
- 命中时写/更新偏好（metadata.label 定位）：更新 = **删旧行 + 写更高置信度**（MemoryManager 无原地更新 API，delete+write）。
- **矛盾信号**（同维度反向）：contradicts+1，置信度下降；已沉淀者低于阈值 → 降级/删除。
- **跨重启累积**：计数器进程内；重启后按 label 在 MemoryManager 中找到已有偏好继续更新置信度（持久化在记忆行）。

## 5. 显式纠正

- **话语纠正**：检测纠正词 → 写显式偏好（source="user", confidence=1.0）并**删除冲突的隐式偏好**（同 dimension 反向，§8.3 显式>隐式）。
- **工具路径**：现有 memory 工具（delete/strengthen）保留，CLI/memory 服务手动纠偏。

## 6. 消费

- **沉淀即记忆**：偏好存 MemoryManager（preference 类型），L2 云端上下文/工具已读（CloudViewBuilder 对 preference 有 MEMORY_MIN_TOKENS 保底）。
- **tuner 冷却偏置**：`FeedbackTuner.set_cooldown_floor(value)` 提高冷却下限；沉淀 "不喜欢频繁主动开口"（confidence ≥ 0.6）时 `floor=120`，环1 调参钳制在该下限之上（不低于 `cooldown_min_s`）。

## 7. 配置

```yaml
sedimenter:
  min_signals: 3
  confidence_threshold: 0.6
  topic_engagement_threshold: 3
```

env：`YUKI_SEDIMENTER_MIN_SIGNALS` / `YUKI_SEDIMENTER_CONFIDENCE_THRESHOLD` / `YUKI_SEDIMENTER_TOPIC_ENGAGEMENT_THRESHOLD`。

## 8. 测试

- `test_sedimenter.py`：各维度信号→计数/置信度；阈值沉淀（hits≥3 且比例≥0.6）；矛盾降级（反向信号→contradicts+1→降级/删）；更新已有偏好（删旧写新、label 定位）；显式纠正删冲突隐式偏好；话题阈值（≥3 次接话同 topic）；显式陈述沉淀（source=user, confidence=1.0）。
- `test_tuner.py`：`detect_polarity` 抽取后行为不变（负/正/中性）；`set_cooldown_floor` 生效（钳制在下限之上）。
- `test_hub.py`：sedimenter 喂入点（on_polarity/on_explicit_statement/on_engagement 被调）；sedimenter=None 行为不变。
- `test_config.py`：sedimenter 节默认值/env。
- e2e 不变（沉淀阈值需 3 次信号，单测才触发；默认不影响现有行为）。

## 9. 风险与兼容

- 零协议变更（REPLY 主题/载荷不变）；零新依赖。
- `detect_polarity` 抽取为纯重构（tuner 行为不变，既有测试保护）。
- 沉淀只写偏好记忆，不改变现有决策路径；tuner 冷却偏置仅在沉淀发生时调整下限。
- **已知限制**：计数器进程内（重启清零，但沉淀结果在记忆行跨重启累积）；话题兴趣为简化 string topic（非细粒度词项）。
- **后续接入点**（明确范围外）：环3 人格快照（偏好并入 soul/快照）、打断/跳页信号接入、细粒度话题建模、偏好记忆的 SQLite 升级。

## 10. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 独立 PreferenceSedimenter 组件 | 环1/环2 解耦、可测；tuner 专注调参 |
| 沉淀即记忆（MemoryManager preference） | 天然持久化；L2 已读；不重复存储 |
| 三维度（节奏/显式/话题） | 直接可用的信号；话题简化为 string topic |
| 计数置信度 + 阈值 | 简单可测；符合 §5.2 置信度语义 |
| 话语 + 工具纠偏 | §8.3 显式>隐式；工具路径保留 |
| tuner 冷却偏置 set_cooldown_floor | 环2 回馈环1 的最小闭环 |
| detect_polarity 抽出共享 | 避免双份极性判定 |
