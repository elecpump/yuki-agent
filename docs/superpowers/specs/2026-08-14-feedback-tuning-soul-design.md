# Yuki 反馈闭环环1: 参数自调 + soul 状态 设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：环1 参数自调——隐式回应 + 显式话语反馈实时调整主动开口冷却；持久化到版本化 soul 状态

## 1. 背景与目标

实现设计文档 `2026-08-10-yuki-agent-design.md` §6 的**环1 参数自调**（短期）：根据用户反馈实时微调主动开口冷却时间，且把调参结果持久化到一个最小化、版本化的 **soul** 状态（环3 人格快照的前身）。

**已确认决策**：
- **信号范围**：隐式回应（主动开口后窗口内是否接话）+ 显式反馈话语（正/负极性关键词）。
- **只调冷却**：`proactive_cooldown_s`（不调篇幅/参与度，留待后续）。
- **引入 soul**：持久化 json，版本化，可一键重置；环1 写入，启动读取，环3 在此之上扩展完整人格快照。
- **soul 只存参数**：不存 persona 提示词/口语风格（归环3）。
- **调参机制参数放代码常量**：窗口/系数/钳制不做配置。
- **无线程**：tuner 惰性检测超时，不引入后台线程。

**范围外**：打断（FocusManager 桩）、跳页检测、档位切换等信号；篇幅/参与度调参；环2 偏好沉淀（重复模式→偏好记忆）；环3 人格快照/演化。

## 2. 架构与文件布局

```
src/yuki/cognition/brain/
  tuner.py      — FeedbackTuner（窗口/显式极性/调参）+ 极性关键词集
  soul.py       — SoulStore（json 读写，版本化，可重置）
src/yuki/config.py  — soul: 节（path）
tests/cognition/
  test_soul.py / test_tuner.py / test_hub.py（增）
```

## 3. soul 状态（soul.py）

```json
{ "persona_name": "yuki", "persona_version": 1,
  "params": { "proactive_cooldown_s": 137.5 },
  "updated_at": "2026-08-14T..." }
```

```python
class SoulStore:
    def __init__(self, path: str | Path, persona_name: str, persona_version: int = 1) -> None: ...
    def load(self) -> dict | None: ...          # 版本不符/损坏 → None（随后重写）
    def save(self, params: dict) -> None: ...   # 写回 {persona_name, persona_version, params, updated_at}
    def reset(self) -> None: ...                # 删除文件（回 config 默认）
```

- 启动：policy 冷却 = `soul.load()` 的 `params.proactive_cooldown_s`（有则用之），否则 config 默认。
- 父目录不存在自动创建；写失败不致命（记录警告）。

## 4. FeedbackTuner（tuner.py）

```python
NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")

class FeedbackTuner:
    def __init__(self, policy, soul, *, window_s: float = 90.0,
                 cooldown_min_s: float = 30.0, cooldown_max_s: float = 600.0) -> None: ...
    def load_soul(self) -> None: ...            # 启动恢复冷却
    def on_proactive_open(self) -> None: ...    # 记录 _open_ts
    def on_user_utterance(self, text: str) -> None: ...  # 极性 + 窗口 → 调参
    def adjust(self, factor: float) -> None: ...  # 冷却 *= factor，钳制，写回 soul
    @property
    def cooldown_s(self) -> float: ...
```

**信号语义**（显式极性分支与窗口接话分支互斥，同一话语只结算一次）：
- `on_proactive_open`：hub 主动开口（SITUATION 触发且 spoke）时记录 `_open_ts = now`。
- `on_user_utterance(text)`：
  1. **惰性超时检查**（先于其他）：若 `_open_ts` 存在且 `now - _open_ts > window_s` → 静默降温 `adjust(1.3)`，清 `_open_ts`。
  2. **显式极性**（elif 链，任一命中即结算并清 `_open_ts`）：
     - 命中负关键词 → `adjust(1.5)`（强降，**任何开口后都适用**，无论是否在窗口内）
     - 命中正关键词 → `adjust(0.8)`（升温）
  3. **窗口接话**：否则若 `_open_ts` 存在且 `now - _open_ts <= window_s` → 接话轻升温 `adjust(0.9)`，清 `_open_ts`。
  4. 其余（无窗口、无极性）→ 不调参。
- `adjust`：`new = clamp(cooldown * factor, cooldown_min_s, cooldown_max_s)`；有变化则 `policy.set_cooldown_s(new)` + `soul.save({"proactive_cooldown_s": new})`。

## 5. DecisionPolicy 改动

- 增 `set_cooldown_s(value: float)`：设置当前冷却（值已由 tuner 钳制）；`_decide_situation` 改用当前可变值。

## 6. DecisionHub 接线

- 构造增 `tuner: FeedbackTuner | None = None`（None → 现有行为完全不变）。
- `_handle`：SITUATION 触发且 spoke → `tuner.on_proactive_open()`；UTTERANCE 处理时（分类后）→ `tuner.on_user_utterance(text)`（不影响本次决策，只影响后续冷却）。
- 装配（`build_brain`/agent）：构造 policy 后 `tuner.load_soul()` 恢复冷却。
- 决策轨迹记录调参事件（`tuner.adjust` 记一条日志）。

## 7. 配置

```yaml
soul:
  path: "data/soul.json"
```

env：`YUKI_SOUL_PATH`。调参机制参数（窗口 90s、系数 0.8/0.9/1.3/1.5、钳制 [30,600]）为代码常量。

## 8. 测试

- `test_soul.py`：写读回、版本不符忽略、损坏 json 容错（None）、reset 删除、父目录创建、save 后 json 形状。
- `test_tuner.py`：窗口内接话升温（0.9）、窗口超时静默降温（1.3）、显式负反馈强降（1.5，窗口外也生效）、显式正反馈升温（0.8）、钳制上下界、adjust 后 policy/soul 同步、load_soul 恢复。
- `test_policy.py`：`set_cooldown_s` 后 `_decide_situation` 用新值（配合 now/last_open 断言）。
- `test_hub.py`：SITUATION spoke → `tuner.on_proactive_open` 被调；UTTERANCE → `on_user_utterance` 被调；`tuner=None` 行为不变。
- e2e 不变：默认 `data/soul.json` 仅在启用 soul/tuner 时产生；无 soul 文件时冷却回 config 默认。

## 9. 风险与兼容

- 零协议变更（REPLY 主题/载荷不变）；零新依赖（stdlib json + 现有 pydantic）。
- `tuner=None` 时行为与现在完全一致；soul 文件缺失/损坏 → 回 config 默认，不崩溃。
- 调参仅冷却且钳制，最坏情况是冷却被调到边界值，可一键 reset。
- **后续接入点**（明确范围外）：环2 偏好沉淀（重复反馈→偏好记忆，soul 与 MemoryManager 对齐）；环3 人格快照（persona 提示词/口语风格并入 soul，版本管理 + 回滚/导入导出）；打断/跳页信号接入 tuner。

## 10. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 隐式回应 + 显式话语两类信号 | 现有信号即可支撑；打断/跳页是桩，留后续 |
| 只调冷却 | 篇幅/参与度需动作层配合，改动面大；冷却最能体现"开口时机"学习 |
| 引入 soul（json 版本化） | 调参要有持久落点；环3 快照的自然前身；可读可重置 |
| soul 只存参数 | 提示词/口语风格归环3，避免本轮范围膨胀 |
| 调参机制参数用代码常量 | YAGNI，先验证机制 |
| tuner 无线程、惰性检测 | 单用户事件量低；避免线程复杂度 |
