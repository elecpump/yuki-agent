# Yuki 上下文工程（WorkingContext + CloudViewBuilder）设计

> 日期：2026-08-14
> 状态：已确认（brainstorming 一轮确认）
> 范围：上下文子系统——维护结构化工作上下文（会话轮次/情境快照），并产出经预算/去重/LLM 摘要折叠的云端提示视图

## 1. 背景与目标

当前上下相关代码三块脱节：`DecisionHub._context` 只有最新情境快照（无历史）；`l2/context.py` 的 `build_cloud_context` 是静态文本拼装（无预算/去重/历史/压缩）；`MemoryManager.short_term` 工作记忆建成但无消费者。本设计统一为**上下文子系统**：`WorkingContext` 维护工作上下文，`CloudViewBuilder` 产出工程化的云端视图。

**已确认决策**：
- **两者都做**：结构化工作上下文 + 预算/压缩的云端提示视图。
- **会话轮次存 ShortTermMemory**：`MemoryManager.short_term_add/items`（TTL 30min/容量 50），经 `TurnStore` 协议抽象（默认 `ShortTermTurnStore`），未来可换 Redis 实现。
- **尽力持久化**：会话轮次定期快照 + 进程退出前 flush 到本地文件（`data/context_snapshot.json`），重启恢复（TTL 过滤）；`snapshot_path` 为 None 时明确接受重启丢失。
- **含 LLM 摘要折叠**：更旧轮次超预算时用云端摘要压缩，带缓存；失败回退计数占位。

**范围外**：会话历史的 SQLite 持久化、环2 偏好沉淀、L2 决策链、跨进程函数服务。

## 2. 架构与文件布局

```
src/yuki/cognition/context/
  store.py     — TurnStore 协议 + ShortTermTurnStore（默认包装 MemoryManager.short_term）
  working.py   — WorkingContext（写入侧：add_user/add_agent/update_situation + 尽力持久化）
  snapshot.py  — ContextSnapshot（只读 frozen）+ ContextProjector（投影/裁剪/排序/去重）
src/yuki/cognition/l2/
  view.py      — CloudViewBuilder（enrich 填充 summaries/long_term_memory + format 产出提示文本；预算/去重/LLM 摘要折叠/缓存）
  bridge.py    — CloudBridge 改用 (utterance, context_snapshot, memory) → view
src/yuki/cognition/brain/hub.py — 用 WorkingContext 写入 + 决策时投影快照替代 self._context
删除: src/yuki/cognition/l2/context.py（build_cloud_context 被 view 取代）
```

- `store.py`/`working.py` 状态管理；`snapshot.py` 投影；`view.py` LLM 适配；`bridge` 仅接线。各组件独立可测。

## 3. WorkingContext（working.py + snapshot.py）

**读写分离，决策取一致快照**：`WorkingContext` 是**写入侧**（hub 追加轮次/情境），`ContextSnapshot` 是**每次决策开始时由投影器生成的只读快照**——决策期间即使 short_term 被异步修改（主动推送/并发 L2），快照不变，上下文一致。

```python
# store.py — 存储接口可替换（多实例/未来 Redis）
class TurnStore(Protocol):
    def add(self, content: str, kind: str, ts: float) -> None: ...
    def items(self) -> list[dict]: ...     # [{"content", "kind", "ts"}]
    def clear(self) -> None: ...

class ShortTermTurnStore:
    """默认实现：包装 MemoryManager.short_term（TTL 30min/容量 50）。"""

# working.py — 写入侧（hub 持有）+ 尽力持久化
class WorkingContext:
    def __init__(self, store: TurnStore, *, snapshot_path: str | Path | None = None,
                 snapshot_interval: int = 5, ttl_s: float = 1800.0) -> None: ...
    def add_user(self, text: str) -> None: ...
    def add_agent(self, text: str) -> None: ...
    def update_situation(self, payload: dict) -> None: ...
    def snapshot(self) -> None: ...   # 尽力持久化（定期）
    def restore(self) -> None: ...    # 启动恢复（TTL 过滤）
    def close(self) -> None: ...      # 进程退出前 flush

# snapshot.py — 只读快照（每次决策投影）+ 清晰 schema
@dataclass(frozen=True)
class ContextSnapshot:
    situation: dict | None = None
    recent_turns: tuple[dict, ...] = ()       # 裁剪/排序后的轮次（新→旧）
    summaries: tuple[str, ...] = ()           # 更旧轮的折叠摘要
    long_term_memory: tuple[dict, ...] = ()   # 检索/过滤后的长期记忆

class ContextProjector:
    def build(self, working: WorkingContext, *, max_turns: int = 20) -> ContextSnapshot: ...
```

- **投影（ContextProjector.build）**：从写入侧读出原始数据，做**裁剪/排序/去重**后填入 `recent_turns`（新→旧，`max_turns` 上限），`situation` 取最新快照。**决策层只见快照 schema，不见 short_term 内部结构**——short_term 结构变更不牵连决策层。
- **LLM 适配（CloudViewBuilder，§4）**：`enrich(snapshot, memory, summarize)` 填充 `summaries`/`long_term_memory`，`format(...)` 产出提示文本。**绝不把 short_term 原始内容直接喂 LLM**，一律经快照适配。
- **并发**：hub 在 `_handle` 开始时 `projector.build(working)` 得快照，本次决策全程用快照（L1 动作读 `situation`/`recent_turns`，L2 路径 enrich+format）。
- **尽力持久化（会话不因重启断崖）**：`snapshot_path` 非 None（默认 `data/context_snapshot.json`）时每 `snapshot_interval` 次 add 后 `snapshot()`、`close()` 写最终快照；`restore()` 读回 → **TTL 过滤过期轮次** → 回填。`snapshot_path` 为 None → 纯内存，明确接受重启丢失。读写失败仅告警。
- **已知限制（明确记录）**：会话轮次为单机状态，多实例不共享；`TurnStore` 协议已预留替换（Redis 实现同协议即可换入）。

## 4. CloudViewBuilder（view.py）

**组装为「填充顺序 + 最低配额」模型**（`priority` 明确为**填充顺序**：高优先级节先放入；每节有最低配额保证，不可因预算被砍）：

```python
def estimate_tokens(text: str) -> int: ...   # ceil(len/1.5)，字符启发式，零依赖

# 配额常量
SITUATION_TOKENS = 200          # 情境固定配额，不可裁剪
MEMORY_MIN_TOKENS = 200         # 关键偏好（preference/strengthened）最小配额
MAX_UTTERANCE_CHARS = 500       # utterance 恒保留但截断到该长度

class CloudViewBuilder:
    def __init__(self, summarize: Callable[[list[str]], str] | None = None, *,
                 max_turns: int = 20, max_tokens: int = 1500,
                 verbatim_turns: int = 4, memory_top_k: int = 3) -> None: ...
    def enrich(self, snapshot: ContextSnapshot, memory: MemoryManager | None,
               utterance: str) -> ContextSnapshot: ...  # 填充 summaries + long_term_memory（折叠/检索）
    def format(self, snapshot: ContextSnapshot, utterance: str) -> str: ...  # 产出提示文本
```

**两阶段：`enrich`（L2 专用，填充 summaries/long_term_memory）→ `format`（预算/排序产出文本）**。输入一律是 `ContextSnapshot`（§3 投影产物），**绝不接触 short_term 原始结构**。

**填充顺序与配额**（参与视图的轮次 = `snapshot.recent_turns`，即投影时已按 `max_turns` 裁剪的工作窗口）：

| 顺序 | 节 | 配额保证 | 预算紧张时的处理 |
|---|---|---|---|
| 1 | **utterance** | 恒保留，`max_utterance_chars` 截断 | 永不裁剪（仅截断长度） |
| 2 | **情境**（situation topic/summary/key_points） | 固定 `SITUATION_TOKENS`，不可裁剪 | 截断过长的 summary/key_points 到配额内 |
| 3 | **逐字轮** | 恒保留最近 `verbatim_turns` 轮 | 永不裁剪（超出的轮转下一节折叠） |
| 4 | **折叠轮**（窗口内更旧的轮） | 填充剩余预算 | **首个被压缩的对象**：摘要 → 更短摘要 → 计数占位 |
| 5 | **记忆** | 关键偏好（`memory_type=="preference"` 或 `strengthened`）保证 `MEMORY_MIN_TOKENS`；其余检索 top-k（`memory_top_k`，**过滤 `sensitivity == 2`**）填充剩余 | 先砍次要检索记忆，再砍偏好配额（但不低于最低值） |

- **预算分配**：先计算固定配额节（utterance+情境+逐字轮+记忆最小配额）的估算总量；剩余预算给折叠轮（优先）与记忆检索节。总超预算时，按上表"预算紧张时的处理"列裁剪（折叠轮 → 记忆检索），固定配额节不动。
- **去重**：连续重复文本（相邻轮次同 content）只保留一次。

**折叠轮（窗口内、逐字轮之外且 ≤ `max_turns` 的轮）——固定折叠单元 + 预算触发 + 缓存 + 熔断**：

```python
FOLD_UNIT_SIZE = 6            # 每 6 轮为一个折叠单元
SUMMARIZE_TIMEOUT_S = 2.0     # 摘要调用独立超时（短于主响应）
SUMMARIZE_MAX_FAILURES = 3    # 连续失败阈值 → 熔断禁用 L2 摘要

# CloudViewBuilder 内部状态
self._summary_cache: dict[str, str] = {}   # 折叠单元键 → 摘要
self._summarize_failures: int = 0          # 连续失败计数
self._summarize_broken: bool = False       # 熔断后置位
```

1. **固定折叠单元**：折叠轮按轮序每 `FOLD_UNIT_SIZE` 轮切成一个单元（最旧单元在前）。**单元内容一旦脱离逐字窗口即固定，新消息只追加到最新单元**——已折叠单元不受影响，缓存命中率高。缓存键 = 该单元轮次内容序列的哈希（`hashlib.sha256`）。
2. **预算触发（不固定轮数触发）**：仅当逐字保留折叠轮文本会超出预算时，才从最旧单元起逐一折叠；预算足够时（短会话）**不调摘要**，减少无谓 LLM 往返。
3. **折叠单元处理**：命中缓存 → 复用；未命中且 `summarize` 可用且未熔断 → 调 `summarize(segment_texts)`（**独立 `SUMMARIZE_TIMEOUT_S` 超时**）→ 缓存摘要；失败/超时 → 计数占位 `（之前聊了 N 轮）`，连续失败计数 +1。
4. **熔断**：`_summarize_failures >= SUMMARIZE_MAX_FAILURES` → `_summarize_broken = True`，之后不再调摘要，一律计数占位（规则截断降级），避免每次响应都被慢/失败的摘要拖累；成功一次重置计数。

## 5. CloudBridge 改动

- `CloudBridge.__init__(client, registry=None, system_prompt=None, max_turns=3, persona_name="yuki", view_builder=None)`。
- `generate(utterance, context: ContextSnapshot | None = None, memory=None) -> str`：
  - `snapshot = view_builder.enrich(context, memory, utterance)`（`context=None` 时退化为仅含 utterance 的快照）；`view = view_builder.format(snapshot, utterance)`。
  - 其余（messages 组装/工具多轮/CloudError 降级）不变。
- 默认 view_builder 的 `summarize` 由 bridge 注入：闭包调 `self._client.chat([system=SUMMARIZE_PROMPT, user=older_texts], timeout_s=SUMMARIZE_TIMEOUT_S)` 取 `choices[0].message.content`；超时/失败向上抛由 view 层回退计数占位并计入熔断。为此 `CloudClient.chat` 增可选 `timeout_s` 参数（缺省用客户端超时），其余不变。
- `SUMMARIZE_PROMPT`（代码常量）：把对话压缩成简短中文摘要，保留关键事实与用户偏好。

## 6. DecisionHub 接线

- `DecisionHub` 增 `context: WorkingContext | None = None` 与 `projector: ContextProjector | None = None`（均 None → 行为不变，`self._context` 照旧）。
- 有 context 时：UTTERANCE → `context.add_user(text)`；spoke → `context.add_agent(rendered)`；SITUATION → `context.update_situation(payload)`。
- **决策开始时投影**：`_handle` 入口 `snapshot = projector.build(context)`，本次决策全程用 `snapshot`（L1 动作读 `snapshot.situation`/`snapshot.recent_turns`；L2 路径 `bridge.generate(utterance, snapshot, memory)`）。并发下快照一致。
- `build_brain`/agent 装配 `WorkingContext`（`ShortTermTurnStore(memory)` + `config.context.snapshot_path`）与 `ContextProjector` 并传给 hub + bridge；`CognitionAgent.teardown` 调 `context.close()` 写最终快照。

## 7. 配置

```yaml
context:
  max_turns: 20
  max_tokens: 1500
  verbatim_turns: 4
  snapshot_path: "data/context_snapshot.json"   # 空串 = 纯内存，接受重启丢失
```

env：`YUKI_CONTEXT_MAX_TURNS` / `YUKI_CONTEXT_MAX_TOKENS` / `YUKI_CONTEXT_VERBATIM_TURNS` / `YUKI_CONTEXT_SNAPSHOT_PATH`。配额常量（`SITUATION_TOKENS`/`MEMORY_MIN_TOKENS`/`MAX_UTTERANCE_CHARS`）与折叠常量（`FOLD_UNIT_SIZE`/`SUMMARIZE_TIMEOUT_S`/`SUMMARIZE_MAX_FAILURES`）、`snapshot_interval` 为代码常量；`memory_top_k` 沿用 `CLOUD_MEMORY_TOP_K`（3）；WorkingContext 轮次容量由 short_term（50）承载。

## 8. 测试

- `test_working.py`：WorkingContext 写侧（add_user/add_agent/update_situation）；snapshot/restore 往返（写文件→新实例恢复→TTL 过滤过期轮次→situation 恢复）；`snapshot_path=None` 时不写文件；快照写失败仅告警。
- `test_snapshot.py`：ContextProjector 投影（新→旧、`max_turns` 裁剪、连续去重、situation 最新快照）；**快照为 frozen 只读**；`TurnStore` 协议可用自定义 store 注入（可替换性验证）。
- `test_view.py`：`enrich`/`format` 两阶段（输入 ContextSnapshot）；填充顺序（utterance→情境→逐字轮→折叠轮→记忆）；固定配额（情境不裁剪、utterance 截断到 MAX_UTTERANCE_CHARS、逐字轮恒保留 verbatim_turns 轮）；预算紧张时先压缩折叠轮再砍次要记忆（关键偏好 `preference`/`strengthened` 保底 MEMORY_MIN_TOKENS）；连续重复去重；**折叠**：固定单元切分、单元缓存命中复用（新轮次不影响已折叠单元）、预算触发（短会话不调摘要）、摘要独立超时、失败计数占位、连续失败 `SUMMARIZE_MAX_FAILURES` 后熔断（不再调摘要、成功重置）；记忆高敏过滤；无快照退化。
- `test_bridge.py`：注入 fake view_builder → generate 用其 enrich/format 输出；默认 builder 装配正常。
- `test_hub.py`：context 写侧喂入（add_user/add_agent/update_situation 被调）+ 决策用投影快照；context=None 行为不变。
- `test_cognition.py`：agent 装配 WorkingContext + ContextProjector。
- e2e 不变（cloud 默认关；context 快照文件默认 `data/context_snapshot.json`，无决策时仅启动 restore 读取、不产生写）。

## 9. 风险与兼容

- 零协议变更（REPLY 主题/载荷不变）；零新依赖（stdlib hashlib/math/json）。
- `context=None` / `view_builder=None` 时行为与现在一致；云摘要失败 → 计数占位，不崩。
- 摘要为启发式折叠，非完整对话记忆；完整对话持久化留待会话记忆模块。
- **尽力持久化是"尽力"**：崩溃时最近未快照的轮次会丢（尾部丢失，非全丢）；快照读写失败仅告警。
- **已知限制（明确记录）**：会话轮次为单机内存状态，多实例不共享；`TurnStore` 协议已预留替换（Redis 等实现同协议即可换入）。
- **后续接入点**（明确范围外）：会话历史 SQLite 持久化、环2 偏好沉淀、摘要质量提升（更长窗口/分级摘要）、L1 侧用 WorkingContext 增强动作（ask 引用历史）、多实例共享存储（Redis TurnStore）。

## 10. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 读写分离：WorkingContext 写侧 + ContextSnapshot 只读快照 | 决策取一致快照，异步改动不影响本次决策；并发安全 |
| 快照清晰 schema（situation/recent_turns/summaries/long_term_memory） | 决策层只见 schema，不隐式耦合 short_term 内部结构 |
| 绝不把 short_term 原始内容直接喂 LLM | LLM 一律经快照适配（enrich/format） |
| WorkingContext 用 short_term（经 TurnStore 协议） | 工作记忆终于有消费者；存储接口可替换（未来 Redis） |
| 尽力持久化（定期快照 + 退出 flush + 重启恢复） | 避免重启断崖；崩溃仅丢尾部 |
| 快照 TTL 过滤 | 恢复时不复活过期轮次 |
| 填充顺序 utterance→情境→逐字轮→折叠轮→记忆 | 面向 LLM 的有效利用；utterance/情境/逐字轮是关键上下文 |
| 「填充顺序 + 最低配额」预算模型 | 高优先级先放、各节有保证配额；折叠轮与次要记忆吸收预算压力，长期画像（关键偏好）不先丢 |
| 字符启发式估 token | 零依赖；够预算管理用 |
| 固定折叠单元（每 6 轮）+ 单元缓存 | 新消息不使已折叠单元失效，缓存命中率高 |
| 预算触发折叠（非固定轮数） | 短会话不调摘要，减少无谓 LLM 往返 |
| 摘要独立短超时（2s） | 摘要延迟不拖垮主响应 |
| 熔断（连续失败 ≥3 禁用摘要，回退计数占位） | 避免每次响应被慢/失败的摘要拖累 |
| LLM 摘要折叠 + 缓存 | 长会话不爆窗口；缓存避免重复调用 |
| 失败回退计数占位 | 摘要不可用不阻塞主响应 |
