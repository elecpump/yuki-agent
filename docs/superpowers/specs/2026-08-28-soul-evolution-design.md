# Soul 演化与工具接口统一 设计

> 日期：2026-08-28
> 状态：已评审（方案可行；评审问题已修订，分阶段实施中）
> 范围：三职责架构定位、Soul 演化机制、旧组件清理清单、实施顺序
> 依赖：`docs/superpowers/specs/2026-08-26-proactive-opening-redesign.md`（ProactiveAgent/CooldownCalculator 草案，本设计的前置）

## 目标

放弃"三环"（参数自调 / 偏好沉淀 / 人格演化）概念，新架构划分为三个独立职责，每个职责一个统一工具接口：

| 职责 | 负责组件 | 状态 |
|---|---|---|
| 频率控制 | `ProactiveAgent` + `CooldownCalculator` | 已设计（草案，**未实现**，本设计的前置依赖） |
| 偏好沉淀 | L1 Agent Loop 的 `memory.*` 工具 | 已实现（`functions/memory_tools.py`） |
| 人格演化 | `soul.update` 工具 + 定期反思任务 | 本次设计 |

三职责共用同一原则：**模型只负责"决策与内容"，规则与存储由代码侧提供**。人格演化中模型产出新的人格内容，但写入、校验、快照、审计全部由代码侧完成。

## 组件现状核对（评审核实）

| 组件 | 代码现状 | 动作 |
|---|---|---|
| `PreferenceSedimenter` | **已删除**（`f18280a` L0/L1 迁移移除） | 无需动作 |
| `Intent` 枚举 | **已删除**（`f18280a` 移除，全库无引用） | 无需动作 |
| `FeedbackTuner`（`brain/tuner.py`） | 存在；`assembly.py:175` 装配、`hub.py:114` 经 `TunerSink` 挂接 | 删除 |
| `TunerSink`（`brain/sink.py`） | 存在；hub 的 `register_sink` 一并删除 | 删除 |
| `DecisionPolicy` 主动开口逻辑（`brain/policy.py`） | 存在；situation 冷却 + binding blocks 硬阻断 | 删除（由硬门 + ProactiveAgent 替代） |

删除项与 2026-08-26 proactive 草案 §10 移除表一致，无冲突。

## Soul 演化机制

### 演化范围（无约束）

- `core_values`：全部可改（**包括 `role=binding` 的价值观**，不再有代码级硬阻断）。
- `personality_traits`：全部可改（5 维，仍按 `_normalize` clamp 到 [0,1]）。
- `personality_description`：全部可改（非空、限长，见 §评审补充 6）。

binding 硬阻断机制（`policy.py:39-47` 的 `blocks`）随 `DecisionPolicy` 删除；binding 价值观从"代码级约束"降级为"prompt 软约束"（与 proactive 草案 §6.3 声明一致）。

### 统一更新接口

实时路径（模型对话中主动调用）与定期路径（后台反思任务）调用**同一个**接口：

```text
soul.update(traits?, core_values?, description?)
```

- `traits`：**局部 patch**；仅接受 `DEFAULT_TRAITS` 5 维键，未知键使整次请求失败，值 clamp [0,1]。
- `core_values`：**全量替换**；逐项校验 `id`（strip 后非空且列表内唯一）/`text`（strip 后非空）/`role`（guiding|binding）/`confidence`（clamp）。任一项不合法则整次请求失败，禁止“忽略坏项后部分覆盖”。
- `description`：str，strip 后非空且不超过 `max_description_chars`（默认 2000）。
- 全部参数可选；至少一项非空才写入。写入前与当前 soul 做 diff，**无实际变化不写、不快照**。
- 写入走 `SoulStore` 现有 `atomic_write_json` + `get_audit_logger`（新增 `soul.update` 审计事件，记录变更字段与来源：`realtime` | `periodic`）。`source` 是代码侧参数，不暴露给模型：工具 wrapper 固定为 `realtime`，反思任务固定为 `periodic`。
- Soul 持久化 `revision`（默认 0，每次实际变更 +1）。定期反思提交时携带开始反思时的 `expected_revision`；版本已变化则将候选标记为 stale 并丢弃，不覆盖较新的实时更新。

### 触发机制

1. **实时路径**：`soul.update` 注册为 `functions/registry` 工具（`functions/soul_tools.py`，仿 `register_memory_functions` 模式），由 L1 Agent Loop 的工具 dispatch 执行。
   - **生效范围限制（评审补充 2）**：本地脑（`LocalComposer`）不执行工具，实时路径**仅对云端 loop 与 Gateway chat 生效**；定期路径不受限。
   - 工具成功提交后在存储锁外触发轻量 prompt refresh（不调用 LLM refine）。工具所在的当前 Agent Loop 已固定 system message，因此更新保证从**下一次用户请求**起对 CloudBridge 和 LocalComposer 同时生效。
2. **定期路径**：后台反思任务，触发条件为**每 N 轮对话**（`soul.reflect_every_utterances`，默认 30）**或固定时间间隔**（`soul.reflect_interval_s`，默认 3600，二者取先到者）。
   - 不直接复用 hub 现有的单一 `periodic_interval`：它只有轮次触发，且所有 callback 共用同一间隔。新增独立 `SoulReflectionScheduler`，同时维护 `utterances_since_reflect` 和 `next_due_at`，由 utterance 通知与可停止的墙钟 timer 共同唤醒；同一时刻只允许一个反思任务在途，两个条件同时到期只提交一次。
   - 反思任务用 `CloudClient` 生成候选更新（prompt：当前 soul + 近期对话/偏好摘要 + 要求输出严格 JSON 的 `{traits?, core_values?, description?}`），解析失败/超时/云端不可用 → 本轮跳过（记 trace，不报错、不打扰）。
   - 反思输入只使用有界 `ContextSnapshot` 和经 `MemoryPurpose.PERSONA_REFINE_CLOUD` 过滤的公开偏好；用户内容作为不可信数据分隔，不接受其中要求绕过 schema/护栏的指令。
   - 反思产出与实时调用走同一 `SoulStore.update(expected_revision=...)`；陈旧候选不重放、不覆盖。

### 并发与线程安全（评审补充 3）

- `SoulStore` 的 `load`/`save`/`ensure`/`reset`/`update`/`restore` 与全部读快照方法共用一把 `threading.RLock`；不得保留绕过锁的公开写路径。
- RLock 只保护内存比较与文件提交，不跨越 LLM 调用或 prompt refresh。反思的读—生成—写竞争由 `revision` + `expected_revision` 解决，而不是误认为写锁能保护整个长事务。

### 存储与版本控制

- 主存储：`data/soul.json`（现有 `SoulStore`，`atomic_write_json` 保证原子写）。
- 快照：在首次更新前保存 revision 0 基线；每个保留版本使用单调 revision 命名 `soul_snapshot_r000001.json`，避免秒级文件名碰撞。快照内容是该 revision 提交后的完整 Soul。快照文件先暂存、主文件随后提交；`restore()` 只接受 `snapshot.revision <= 当前主文件 revision`，所以两次原子写之间崩溃留下的未来孤儿快照不可恢复，并会被后续同 revision 提交覆盖。
- **回滚护栏（评审补充 5）**：提供 `SoulStore.restore(revision)`；恢复操作本身生成新的 revision 和审计事件，而不是让当前 revision 倒退。后续 CLI 只包装此 API，并在文档给出手动恢复说明。
- **快照节流（评审补充 4）**：diff 无变化不写；`min_snapshot_interval_s` 窗口内只保留最新提交版本，中间 revision 仍在审计日志中但不承诺可逐版本恢复。`min_snapshot_interval_s=0` 时每个 revision 均保留。超限按 revision 删除最旧版本，严格最多保留 `max_versions`；配置允许 `max_versions=1`，此时不保证保留 revision 0 基线。
- `restore()` 与普通更新一样触发运行时 prompt refresh，且写 `soul.restore` 审计事件。

### 危机段保护（评审补充 6）

"无约束"下模型可改写 `cv.safety`（自伤/自杀关怀）与 description 中的危机句。**接受此产品决策，但保留护栏**：

- `description` 空串/纯空白/超长 → 拒绝写入（`update()` 返回错误）。
- `core_values` 空列表 → 拒绝（当前 `_normalize` 会回退 `INITIAL_CORE_VALUES`，显式拒绝更清晰）。
- 每次 `soul.update` 写审计日志（含来源与字段级 diff），可回溯"人格是什么时候被谁改的"。
- 如需更强保护，后续可加"description 必须包含危机关怀句"的校验——本期不做（无约束决策），仅预留钩子。

## 配置变更

```python
class SoulConfig(BaseModel):
    path: str = "data/soul.json"
    tuner_state_path: str = "data/tuner_state.json"   # 删除；改为 cooldown_state_path（见下）
    snapshots_dir: str = "data/soul_snapshots"
    max_versions: int = Field(50, ge=1)
    min_snapshot_interval_s: float = Field(60.0, ge=0.0)
    max_description_chars: int = Field(2000, ge=100)
```

`reflect_every_utterances`/`reflect_interval_s` 在 `SoulReflectionScheduler` 实施时与调度器一并加入，首批实现不提前落死配置。

- `SoulConfig.tuner_state_path` → `cooldown_state_path`（proactive 草案 §11/§12 已规划；`CooldownCalculator` 启动时迁移旧 `tuner_state.json` 一次）。
- `soul.json` 遗留字段 `prefs_since_regen`（已删 Sedimenter 的遗留）在下次 `SoulStore.save` 时移除（`default_soul`/`_normalize` 不再产出该字段；已存在文件由 `_normalize` 丢弃）。

## 旧组件清理清单

| 组件 | 动作 | 连带 |
|---|---|---|
| `brain/tuner.py`（`FeedbackTuner`） | 删除 | `detect_polarity` 与正/负向词表迁移至 `brain/cooldown.py`（proactive 草案 §5.4） |
| `brain/sink.py`（`TunerSink`/`DecisionSink`） | 删除 | hub 的 `register_sink`/sink 列表删除 |
| `brain/policy.py`（`DecisionPolicy`/`SituationAction`） | 删除 | `TriggerKind` 保留，移入 `hub.py`（trace 使用）；`SituationAction` 随旧情境模板删除 |
| `assembly.py` / `hub.py` 接线 | 修改 | `build_brain` 参数：删 `policy`/`tuner`，增 `proactive_agent`/`cooldown_calculator`/`proactive_tick_s`（proactive 草案 §10） |
| `TunerStateStore`（`soul.py`） | 删除 | 由 `CooldownCalculator` 持久化替代 |
| 测试 | 删除/改写 | `test_policy.py`、tuner/sink 相关测试删除；新增 `test_cooldown.py`/`test_proactive.py`/`test_hub_proactive.py`（proactive 草案 §13） |

## 实施顺序（依赖约束）

**顺序不可颠倒**：`FeedbackTuner`/`DecisionPolicy` 删除后、`ProactiveAgent` 落地前，主动开口冷却完全失效（回归）。正确顺序：

1. **ProactiveAgent 前置**：按 2026-08-26 草案实现 `cognition/brain/cooldown.py` + `cognition/l2/proactive.py` + hub 硬门/异步 worker/破冰 tick + 配置扩展 + 测试（草案 §4-§8、§11、§13）。
2. **旧组件清理**：删 `tuner.py`/`sink.py`/`policy.py` 及 `assembly.py`/`hub.py` 接线，`tuner_state` → `cooldown_state` 迁移（草案 §12）。
3. **SoulStore 扩展**：`update()`（diff + RLock + revision/CAS）+ `soul_snapshots/`（节流 + max_versions）+ `restore()` + 遗留字段清理。
4. **soul.update 工具**：`functions/soul_tools.py` 注册 + 工具 schema（仿 memory 工具）；L1 loop 自动获得；成功提交后执行轻量 prompt refresh，使下一次请求生效。
5. **定期反思任务**：新增独立 `SoulReflectionScheduler`（轮次通知 + 墙钟 timer + 单任务在途去重），产出走同一 `update(expected_revision=...)`。
6. **测试与文档**：soul 单测（update/diff/快照/回滚/并发）、工具契约测试、定期任务测试；README 与本文档归档。

### 首批实施状态（2026-08-28）

- 已完成：`SoulStore.update()` 的 RLock、revision/CAS 与存储协调；严格 patch/replace 校验拆至 `soul_contract.py`，版本暂存/节流/剪枝/恢复拆至 `soul_versions.py`；`prefs_since_regen` 已从新格式移除。
- 已完成：`soul.update` 工具注册，`source=realtime` 由 wrapper 固定；工具仅返回 `{updated: bool}`，内部 revision/diff 不暴露；成功提交后执行无 LLM refine 的轻量 prompt refresh，下一次请求生效。
- 已完成：persona prompt 同时注入 description、core values 与 traits；description 中已有的派生段按段落标题识别，内容相同则保持、内容陈旧则原位替换，避免重复标题或子串误判。
- 待实施：ProactiveAgent 前置与旧 tuner/sink/policy 清理；独立 `SoulReflectionScheduler`、反思 CloudClient 调用及生命周期接线；restore CLI/手动恢复文档。

## 测试计划

- `tests/cognition/test_soul_update.py`：traits 非法键拒绝/clamp；core_values 原子全量校验（缺/重复 id、缺 text、role 非法、空列表拒绝）；description 空/超长拒绝；diff 无变化不写不快照；revision/CAS 拒绝陈旧反思；快照节流合并；`restore()` 生成新 revision；全部读写路径串行；`prefs_since_regen` 不再产出。
- `tests/functions/test_soul_tools.py`：工具 schema；payload 校验；审计事件；来源标记（realtime/periodic）。
- `tests/cognition/test_soul_reflect.py`：独立调度器按轮次/时间取先到者且同时到期只执行一次；生命周期停止；`CloudClient` 失败/超时/非法 JSON → 跳过不报错；慢速反思期间实时更新 → stale 候选丢弃；云端输入经过长度限制和隐私过滤。
- 回归：`test_policy.py`/tuner/sink 相关测试删除后全仓 `pytest` 全绿；`is_crisis` 分流与 `CRISIS_FALLBACK_REPLY` 测试必须保留；e2e 断言不变（REPLY/订阅主题无协议变更）。
- proactive 草案 §13 的 cooldown/proactive/hub 测试作为第 1 步验收。

## 风险与已知限制

| 风险 | 缓解 |
|---|---|
| 删除旧组件先于 ProactiveAgent 落地 → 冷却真空 | 实施顺序硬约束（§实施顺序），第 2 步必须等第 1 步验收 |
| 模型自改人格失控（无约束） | 快照 + `restore()` + 审计（字段级 diff）；description/core_values 空值拒绝 |
| 实时高频 update 写放大 | diff 不写 + 快照节流合并 |
| 实时/定期并发写及陈旧反思覆盖 | 所有文件访问经 RLock；反思提交额外使用 revision/CAS 拒绝 stale 候选 |
| `soul.update` 仅云端 loop 生效 | 文档明示；本地脑路径无工具执行，属预期限制 |
| binding blocks 硬阻断移除 | 人格内容允许演化，但保留独立的 `is_crisis` 分流和 `CRISIS_FALLBACK_REPLY` 代码级兜底；预留 description 内容校验钩子 |

## 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 三职责三接口：ProactiveAgent / memory 工具 / soul.update | 替代"三环"；每职责单一工具接口，规则与存储留在代码侧 |
| binding 价值观"无约束，全部可改" | 产品决策；硬阻断机制随 DecisionPolicy 删除，与 proactive 草案 §6.3 降级声明一致 |
| 实时与定期共用同一 `soul.update` | 单一写入路径，校验/快照/审计单点 |
| 快照必配回滚（`soul_snapshots/` + `restore()`） | 保留原 ADR"可回滚"护栏；演化 ≠ 不可逆漂移 |
| `update()` diff + 节流 | 控制写放大与快照膨胀 |
| 先实现 ProactiveAgent，后删 tuner/sink/policy | 避免主动开口冷却真空回归 |
| `prefs_since_regen`/`TunerStateStore` 清理 | 已删 Sedimenter/旧 Tuner 的遗留字段与存储 |
