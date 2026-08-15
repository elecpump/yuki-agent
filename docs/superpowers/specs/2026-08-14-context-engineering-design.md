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
  working.py   — WorkingContext（会话轮次经 MemoryManager.short_term、情境快照）
src/yuki/cognition/l2/
  view.py      — CloudViewBuilder（优先级/预算/去重/LLM 摘要折叠/缓存）
  bridge.py    — CloudBridge 改用 (utterance, context, memory) → view
src/yuki/cognition/brain/hub.py — 用 WorkingContext 替代 self._context
删除: src/yuki/cognition/l2/context.py（build_cloud_context 被 view 取代）
```

- `working.py` 纯状态管理；`view.py` 纯文本组装；`bridge` 仅接线。各组件独立可测。

## 3. WorkingContext（working.py）

**存储接口可替换**（多实例/未来 Redis 演进）：`TurnStore` 协议抽象会话轮次存取，WorkingContext 依赖协议而非 MemoryManager；默认实现 `ShortTermTurnStore` 包装 `MemoryManager.short_term`。

```python
class TurnStore(Protocol):
    def add(self, content: str, kind: str, ts: float) -> None: ...
    def items(self) -> list[dict]: ...     # [{"content", "kind", "ts"}]
    def clear(self) -> None: ...

class ShortTermTurnStore:
    """默认实现：包装 MemoryManager.short_term（TTL 30min/容量 50）。"""

class WorkingContext:
    def __init__(self, store: TurnStore, *, snapshot_path: str | Path | None = None,
                 snapshot_interval: int = 5, ttl_s: float = 1800.0) -> None: ...
    def add_user(self, text: str) -> None: ...        # store.add(text, "turn", now)
    def add_agent(self, text: str) -> None: ...       # 同上
    def update_situation(self, payload: dict) -> None: ...  # 存最新情境快照
    def recent_turns(self, n: int) -> list[dict]: ... # 新→旧
    def situation(self) -> dict | None: ...
    def turn_count(self) -> int: ...
    def snapshot(self) -> None: ...                   # 尽力持久化到 snapshot_path
    def restore(self) -> None: ...                    # 启动恢复（TTL 过滤）
    def close(self) -> None: ...                      # 进程退出前 flush 最终快照
```

- 轮次 = 工作记忆（`ShortTermTurnStore` 包装 short_term），`kind="turn"` 区分于其他事件。
- **尽力持久化（会话不因重启断崖）**：
  - `snapshot_path` 非 None（默认 `data/context_snapshot.json`）时：每 `snapshot_interval` 次 add 后 `snapshot()`；`close()`（进程退出前，经 `CognitionAgent.teardown`）写最终快照。
  - `snapshot()` 写 `{turns: [{content, kind, ts}], situation, saved_at}`；`restore()` 读回 → **按 `ttl_s` 过滤过期轮次** → 经 `store.add` 回填 → 恢复 situation。
  - `snapshot_path` 为 None → 不持久化，明确接受重启丢失（纯内存模式）。
  - 快照读写失败仅告警（尽力而为），不影响主流程。
- **已知限制（明确记录）**：会话轮次为单机状态，多实例间不共享；`TurnStore` 协议已预留替换（未来 Redis 实现同协议即可换入，无需改 WorkingContext）。

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
    def build(self, utterance: str, context: WorkingContext | None,
              memory: MemoryManager | None) -> str: ...
```

**填充顺序与配额**（参与视图的轮次 = `context.recent_turns(max_turns)`，即最近 `max_turns` 轮为工作窗口）：

| 顺序 | 节 | 配额保证 | 预算紧张时的处理 |
|---|---|---|---|
| 1 | **utterance** | 恒保留，`max_utterance_chars` 截断 | 永不裁剪（仅截断长度） |
| 2 | **情境**（situation topic/summary/key_points） | 固定 `SITUATION_TOKENS`，不可裁剪 | 截断过长的 summary/key_points 到配额内 |
| 3 | **逐字轮** | 恒保留最近 `verbatim_turns` 轮 | 永不裁剪（超出的轮转下一节折叠） |
| 4 | **折叠轮**（窗口内更旧的轮） | 填充剩余预算 | **首个被压缩的对象**：摘要 → 更短摘要 → 计数占位 |
| 5 | **记忆** | 关键偏好（`memory_type=="preference"` 或 `strengthened`）保证 `MEMORY_MIN_TOKENS`；其余检索 top-k（`memory_top_k`，**过滤 `sensitivity == 2`**）填充剩余 | 先砍次要检索记忆，再砍偏好配额（但不低于最低值） |

- **预算分配**：先计算固定配额节（utterance+情境+逐字轮+记忆最小配额）的估算总量；剩余预算给折叠轮（优先）与记忆检索节。总超预算时，按上表"预算紧张时的处理"列裁剪（折叠轮 → 记忆检索），固定配额节不动。
- **去重**：连续重复文本（相邻轮次同 content）只保留一次。
- **折叠轮**（窗口内、逐字轮之外且 ≤ `max_turns` 的轮）：非空时——
  - 缓存键 = 该段轮次内容的哈希；命中 → 复用缓存摘要。
  - 未命中：`summarize` 非 None → 调 `summarize(older_texts)` 得摘要并缓存；`summarize` 为 None 或抛异常 → 计数占位 `（之前聊了 N 轮）`，不失败。
  - 若折叠摘要本身超预算 → 截断摘要；仍超 → 降为计数占位。

## 5. CloudBridge 改动

- `CloudBridge.__init__(client, registry=None, system_prompt=None, max_turns=3, persona_name="yuki", view_builder=None)`。
- `generate(utterance, context: WorkingContext | None = None, memory=None) -> str`：
  - `view = (view_builder or CloudViewBuilder()).build(utterance, context, memory)`；无 context 时退化为纯 utterance 视图。
  - 其余（messages 组装/工具多轮/CloudError 降级）不变。
- 默认 view_builder 的 `summarize` 由 bridge 注入：闭包调 `self._client.chat([system=SUMMARIZE_PROMPT, user=older_texts])` 取 `choices[0].message.content`；失败向上抛由 view 层回退计数占位。
- `SUMMARIZE_PROMPT`（代码常量）：把对话压缩成简短中文摘要，保留关键事实与用户偏好。

## 6. DecisionHub 接线

- `DecisionHub` 增 `context: WorkingContext | None = None`（None → 行为不变，`self._context` 照旧）。
- 有 context 时：UTTERANCE → `context.add_user(text)`；spoke → `context.add_agent(rendered)`；SITUATION → `context.update_situation(payload)`；`_handle` 读取情境改经 `context.situation()`。
- `build_brain`/agent 装配 `WorkingContext`（`ShortTermTurnStore(memory)` + `config.context.snapshot_path`）并传给 hub + bridge；`CognitionAgent.teardown` 调 `context.close()` 写最终快照。

## 7. 配置

```yaml
context:
  max_turns: 20
  max_tokens: 1500
  verbatim_turns: 4
  snapshot_path: "data/context_snapshot.json"   # 空串 = 纯内存，接受重启丢失
```

env：`YUKI_CONTEXT_MAX_TURNS` / `YUKI_CONTEXT_MAX_TOKENS` / `YUKI_CONTEXT_VERBATIM_TURNS` / `YUKI_CONTEXT_SNAPSHOT_PATH`。配额常量（`SITUATION_TOKENS`/`MEMORY_MIN_TOKENS`/`MAX_UTTERANCE_CHARS`）与 `snapshot_interval` 为代码常量；`memory_top_k` 沿用 `CLOUD_MEMORY_TOP_K`（3）；WorkingContext 轮次容量由 short_term（50）承载。

## 8. 测试

- `test_working.py`：add_user/add_agent/update_situation/recent_turns 顺序（新→旧）与 kind、turn_count、situation 快照；**snapshot/restore 往返（写文件→新实例恢复→TTL 过滤过期轮次→situation 恢复）**；`snapshot_path=None` 时不写文件；快照写失败仅告警；`TurnStore` 协议可用自定义 store 实现注入（可替换性验证）。
- `test_view.py`：填充顺序（utterance→情境→逐字轮→折叠轮→记忆）；固定配额（情境不裁剪、utterance 截断到 MAX_UTTERANCE_CHARS、逐字轮恒保留 verbatim_turns 轮）；预算紧张时先压缩折叠轮再砍次要记忆（关键偏好 `preference`/`strengthened` 保底 MEMORY_MIN_TOKENS）；连续重复去重；折叠三态（缓存命中复用 / 调 summarize / summarize=None 或异常→计数占位）；记忆高敏过滤；无 context 退化。
- `test_bridge.py`：注入 fake view_builder → generate 用其输出；默认 builder 装配正常。
- `test_hub.py`：context 喂入（add_user/add_agent/update_situation 被调）；context=None 行为不变。
- `test_cognition.py`：agent 装配 WorkingContext。
- e2e 不变（cloud 默认关；context 仅内存）。

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
| WorkingContext 用 short_term（经 TurnStore 协议） | 工作记忆终于有消费者；存储接口可替换（未来 Redis） |
| 尽力持久化（定期快照 + 退出 flush + 重启恢复） | 避免重启断崖；崩溃仅丢尾部 |
| 快照 TTL 过滤 | 恢复时不复活过期轮次 |
| 填充顺序 utterance→情境→逐字轮→折叠轮→记忆 | 面向 LLM 的有效利用；utterance/情境/逐字轮是关键上下文 |
| 「填充顺序 + 最低配额」预算模型 | 高优先级先放、各节有保证配额；折叠轮与次要记忆吸收预算压力，长期画像（关键偏好）不先丢 |
| 字符启发式估 token | 零依赖；够预算管理用 |
| LLM 摘要折叠 + 缓存 | 长会话不爆窗口；缓存避免每次多一次 LLM 往返 |
| 失败回退计数占位 | 摘要不可用不阻塞主响应 |
