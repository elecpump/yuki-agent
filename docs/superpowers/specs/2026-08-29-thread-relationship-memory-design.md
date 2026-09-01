# Yuki Thread + 用户级记忆（跨天/周持续关系）设计

> 日期：2026-08-29
> 状态：设计修订定稿，待实现
> 范围：单用户、单持久 Thread 的对话流持久化（Segment/Episode 双轴）+
> LLM-only Sedimenter + 全自动候选记忆演进 + 记忆冲突更新；cloud 默认开启
> 术语：见 `CONTEXT.md`（Thread / Episode / Segment / Sedimenter / Scenario /
> Preference / Strengthened）

## 0. 设计边界与系统不变量

本设计接受以下产品前提，不在本阶段展开：

- 单机单用户；不设计多用户隔离和租户模型。
- 完整 Episode 可交给已配置的 cloud LLM；不把云端数据出站风险作为本阶段阻断项。
- Thread、记忆候选、人格和 Soul 的演进均由 agent 内部维护，对用户隐藏。
- 不新增让用户查看、确认、编辑、强化、删除或回滚记忆/人格的产品交互；
  gateway 亦不暴露记忆/灵魂管理端点（`/api/memory*`、`/api/soul` 已移除）；
  内部 CLI/诊断能力保留为运维边界（2026-08-30 收紧）。
  用户在普通对话中的表达只是证据，不能绕过自动演进策略直接执行 mutation。

系统必须满足以下不变量：

1. 用户输入先持久化，再开始生成回复；生成失败不能导致输入丢失。
2. LLM 只提出 `MemoryCandidate`，不能直接修改活跃记忆、Persona 或 Soul。
3. candidate 不进入回复上下文、Persona 生成或 SoulReflector 输入。
4. 候选接受、强化、替换和 tombstone 全由确定性的状态机执行，无用户审批步骤。
5. 一次 Episode 的候选落库与巩固水位线推进必须原子、可重放且不重复生效。
6. 记忆更新保留证据和版本历史；模型不得直接物理删除记忆。

## 1. 背景与目标

Yuki 是娱乐陪伴型 agent，用户期望跨天/周被记住。现状缺口：

- 对话轮次 = 内存 + 单文件快照的尽力缓存：`WorkingContext` 使用 30min TTL
  全局单缓冲；崩溃丢尾部，重启会过滤过期轮次。
- 当前 `DecisionHub` 在回复生成完成后才写入用户轮次；生成期间崩溃仍会丢输入。
- recorder `events.jsonl` 是原始事件日志，不能作为可恢复上下文。
- 长期记忆已是用户级、无 Thread 绑定，但没有候选态、证据、版本或冲突更新语义。
- 当前没有自动记忆提取器；旧 `PreferenceSedimenter` 已在 L0/L1 agent-loop 迁移中退役。

目标：

- 对话原文可靠持久化，并以 Segment/Episode 两个正交维度组织。
- 在有界上下文预算内提供跨天连续性，而不是把全部历史摘要塞入 prompt。
- 通过 LLM 提取候选，通过可审计的自动状态机演进长期记忆。
- 支持冲突、替换、失效和人格演进，同时避免一次错误推断永久污染 agent。
- 为垂直领域预留 prompt 和校验扩展点。

## 2. 核心决策

| # | 决策 |
|---|---|
| Q1 | 复用现有活跃记忆类型：事件 → `scenario`，稳定偏好/事实 → `preference`，显式个人事实 → `personal`；新增 candidate/history 表，不新增 `memories.memory_type` 枚举值。 |
| Q2 | Sedimenter 只用 LLM 做内容理解，无规则提取兜底；确定性代码仍负责 schema 校验、权限边界、证据累计、状态迁移和幂等。 |
| Q3 | 新提取的 preference/personal 默认是 candidate，不参与任何 agent 推理；满足自动晋升策略后才写入/更新活跃 `memories`。 |
| Q4 | `scenario` 可在证据落地且校验通过后自动激活，但不默认 strengthened；preference/personal 的激活和强化要求更高证据门槛。 |
| Q5 | Thread turns 在 SQLite 中逐轮持久化，不设 TTL；正文 append-only，不因 Segment 关闭或 Episode 巩固而裁剪。 |
| Q6 | Segment 按长度切分，是上下文压缩单位；Episode 按用户发起和闲置时间切分，是记忆巩固单位。两个游标正交。 |
| Q7 | 使用专用 `ThreadMaintenanceScheduler` 完成 Episode 关闭、Segment 摘要和巩固；不复用请求级 `l2.AgentLoop`。 |
| Q8 | 记忆冲突由 LLM 提议、状态机校验；update 生成新 revision 并 supersede 旧版本，delete 转为 tombstone，不物理删除。 |
| Q9 | Thread、candidate、记忆和 Soul 演进不向用户提供直接操作入口；内部管理/诊断工具不属于用户交互面。 |
| Q10 | `cloud.enabled` 默认 true；缺少 API key 时在装配阶段直接判定 unavailable，走 L1 和持久历史降级，不反复发起必失败请求。 |

## 3. 总体架构

```text
ChatRequest
  → 立即持久化 user turn（response_state=pending）
  → ContextProjector.build(exclude_turn_id=current_user_turn)
      → 最近历史 Segment summaries（有界）
      → 活跃 Segment verbatim（有界）
      → summary 失败时的最近原文降级（有界）
  → LocalViewBuilder / CloudViewBuilder
      → situation → history summaries → recent turns → active memories → utterance
  → LLM
  → 持久化 agent turn，并把 user turn 标为 completed/failed/interrupted

ThreadMaintenanceScheduler（独立 worker、single-flight、lease）
  → 关闭闲置 Episode
  → 摘要已关闭 Segment
  → Sedimenter.consolidate(closed Episode)
      → MemoryCandidate[]
      → 原子保存 candidates + 推进 Episode 水位线
  → MemoryEvolver.evaluate()
      → 自动 accept/reject/supersede/tombstone
      → active memories / memory_history
      → embedding outbox
```

### 3.1 写入时序

用户输入不能继续沿用当前“回复后才写入”的时序：

1. 收到用户 utterance 后，在短事务中解析 Segment/Episode 并写入 user turn。
2. 当前 utterance 单独传给模型；投影时用 `exclude_turn_id` 避免重复注入。
3. 回复成功后追加 agent turn，设置 `reply_to_turn_id`，并将 user turn 的
   `response_state` 更新为 `completed`。
4. 超时、中断或异常时保留 user turn，并记录 `failed`/`interrupted`。
5. proactive agent turn 使用 `source=proactive`；若没有活跃的用户发起 Episode，
   `episode_id=NULL`，不会单独触发记忆巩固。

## 4. 数据模型

以下表与 `memories` 使用同一 SQLite 数据库。实现必须启用 foreign keys、WAL 和
busy timeout，并为所有状态扫描路径建立索引。

```sql
CREATE TABLE threads (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    created_at   REAL NOT NULL,
    last_turn_at REAL
);

CREATE TABLE segments (
    id               INTEGER PRIMARY KEY,
    thread_id        INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    state            TEXT NOT NULL CHECK (state IN ('active','closed')),
    summary          TEXT,
    summary_state    TEXT NOT NULL DEFAULT 'pending'
                     CHECK (summary_state IN ('pending','running','ok','placeholder')),
    summary_attempts INTEGER NOT NULL DEFAULT 0,
    first_turn_id    INTEGER,
    last_turn_id     INTEGER,
    created_at       REAL NOT NULL,
    closed_at        REAL
);

CREATE TABLE episodes (
    id                 INTEGER PRIMARY KEY,
    thread_id          INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    state              TEXT NOT NULL
                       CHECK (state IN ('active','closed','consolidating','consolidated')),
    first_user_turn_id INTEGER,
    last_turn_id       INTEGER,
    started_at         REAL NOT NULL,
    last_activity_at   REAL NOT NULL,
    ended_at           REAL,
    consolidated_at    REAL
);

CREATE TABLE thread_turns (
    id               INTEGER PRIMARY KEY,
    thread_id        INTEGER NOT NULL DEFAULT 1 REFERENCES threads(id),
    role             TEXT NOT NULL CHECK (role IN ('user','agent')),
    source           TEXT NOT NULL CHECK (source IN ('user_input','agent_reply','proactive')),
    content          TEXT NOT NULL,
    ts               REAL NOT NULL,
    request_id       TEXT,
    reply_to_turn_id INTEGER REFERENCES thread_turns(id),
    response_state   TEXT
                     CHECK (response_state IN ('pending','completed','failed','interrupted')),
    segment_id       INTEGER NOT NULL REFERENCES segments(id),
    episode_id       INTEGER REFERENCES episodes(id)
);

CREATE TABLE consolidation_runs (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL UNIQUE REFERENCES episodes(id),
    state          TEXT NOT NULL CHECK (state IN ('pending','leased','completed','failed')),
    lease_until    REAL,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    prompt_version TEXT,
    response_json  TEXT,
    last_error     TEXT,
    updated_at     REAL NOT NULL
);

CREATE TABLE memory_candidates (
    id                  INTEGER PRIMARY KEY,
    episode_id          INTEGER NOT NULL REFERENCES episodes(id),
    draft_key           TEXT NOT NULL,
    proposed_op         TEXT NOT NULL CHECK (proposed_op IN ('add','update','delete')),
    memory_type         TEXT NOT NULL
                        CHECK (memory_type IN ('preference','personal','scenario')),
    canonical_key       TEXT NOT NULL,
    canonical_key_norm  TEXT NOT NULL,
    content             TEXT NOT NULL,
    confidence          REAL NOT NULL,
    sensitivity         INTEGER NOT NULL CHECK (sensitivity IN (0,1,2)),
    target_id           INTEGER,
    target_revision     INTEGER,
    evidence_json       TEXT NOT NULL,
    metadata            TEXT NOT NULL DEFAULT '{}',
    state               TEXT NOT NULL DEFAULT 'candidate'
                        CHECK (state IN ('candidate','accepted','rejected','applied')),
    created_at          REAL NOT NULL,
    evaluated_at        REAL,
    UNIQUE (episode_id, draft_key)
);

CREATE TABLE memory_history (
    id             INTEGER PRIMARY KEY,
    memory_id      INTEGER NOT NULL,
    revision       INTEGER NOT NULL,
    operation      TEXT NOT NULL CHECK (operation IN ('create','update','supersede','tombstone')),
    snapshot_json  TEXT NOT NULL,
    candidate_id   INTEGER REFERENCES memory_candidates(id),
    created_at     REAL NOT NULL,
    UNIQUE (memory_id, revision)
);

CREATE TABLE memory_key_aliases (
    id               INTEGER PRIMARY KEY,
    memory_type      TEXT NOT NULL,
    alias_norm       TEXT NOT NULL,
    canonical_norm   TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    created_at       REAL NOT NULL,
    UNIQUE (memory_type, alias_norm, resolver_version)
);

CREATE TABLE embedding_outbox (
    memory_id INTEGER PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('upsert','delete')),
    queued_at REAL NOT NULL
);
```

建表顺序必须处理 Segment/Episode 与 turn 的循环引用：先创建表，再用应用层事务保证
`first_turn_id`/`last_turn_id` 指向同一分区内的 turn；SQLite foreign-key 可只用于无循环
或可延迟验证的边。正文 append-only，但状态、水位线和边界游标允许更新。

现有 `memories` 表新增：

- `state`: `active|superseded|tombstoned`，默认 `active`；所有正常检索只读 active。
- `revision`: 乐观并发版本，默认 1。
- `updated_at`: 最近一次内容或状态变更时间。
- `supersedes_id`: 新版本替换的旧 memory id，可空。

必须建立至少以下索引：

- `thread_turns(thread_id, id)`、`thread_turns(episode_id, id)`、
  `thread_turns(segment_id, id)`
- `segments(thread_id, state, id)`
- `episodes(state, last_activity_at)`
- `memory_candidates(state, memory_type, canonical_key_norm)`
- `memory_key_aliases(memory_type, alias_norm, created_at)`

### 4.1 Memory 可见性与历史清理

- `MemoryStore.query/search/list`、`MemoryEmbeddingIndexer` 使用的
  `vector_rows/vector_index_state`、MemoryAccess、Persona、SoulReflector 和 decay cleanup
  默认全部增加 `state='active'` 条件。只有内部管理接口可以显式读取其他状态。
- memory 状态变为 superseded/tombstoned 时立即排入 embedding delete outbox；即使 outbox
  尚未处理，`vector_rows` 的 active 条件也必须保证旧向量不可召回。
- superseded 行在 `superseded_retention_days` 后可以物理删除，但必须先确认
  `memory_history` 已保存完整快照且 embedding delete 已完成。`memory_history.memory_id` 和
  `memory_candidates.target_id` 因此是保留原始 ID 的逻辑引用，不设置会阻止清理的外键。
- tombstoned 行默认永久保留（`tombstone_retention_days=0`），不进入回复检索，但
  CandidateResolver 会读取它们以阻止同一失效事实被意外重新创建。非零保留期属于显式
  运维配置。
- active memory 继续走现有 decay cleanup；candidate/history 使用独立保留策略，不能被
  active-memory cleanup 顺带删除。

### 4.2 Segment/Episode 状态推进

- 同一时刻只能有一个 active Segment 和一个用户发起的 active Episode。
- 收到 user turn 时，在同一事务中检查上一 Episode 的 `last_activity_at`：超过
  `episode_idle_s` 则关闭旧 Episode 并创建新 Episode，否则续接。
- agent reply 继承对应 user turn 的 Episode；proactive-only turn 不创建 Episode。
- Segment 每累计 `segment_max_turns` 条即关闭并创建新 Segment，不等待 Episode 结束。
- 启动恢复时修复失去 lease 的 `running`/`consolidating` 状态，但不修改 turn 正文。

## 5. 上下文投影与摘要

`ContextProjector`、`CloudViewBuilder` 和 `LocalViewBuilder` 都必须修改；不能只替换
WorkingContext 的存储源。

`ContextSnapshot` 提供：

- `recent_turns`：活跃 Segment 的最近 `segment_verbatim_max` 条原文。该字段是投影层
  的候选池，不等同于 Builder 的恒保留轮数。
- `summaries`：最近关闭 Segment 的有效摘要，按新到旧、受数量和 token 双重限制。
- `fallback_turns`：摘要 pending/placeholder 时，从最近关闭 Segment 取出的有界原文。
- `long_term_memory`：仅 active memories；candidate/superseded/tombstoned 永不进入。

统一 prompt 数据顺序：

1. System prompt + Soul（由调用层单独提供）
2. 当前 situation
3. 历史 Segment summaries / fallback turns
4. 活跃 Segment verbatim
5. 相关 active memories
6. 当前 utterance

所有 turn、summary 和 memory 都以“历史数据”边界包装，不能被当作新的系统指令。

Segment 关闭后由 scheduler 调用 LLM summarizer：

- summary 必须记录 model、prompt_version、first/last turn id。
- 失败保留 `pending` 并重试；达到 `summary_failures_max` 后写 placeholder。
- placeholder 不承担连续性；Projector 同时启用最近原文 fallback。
- 不向 prompt 注入全部历史摘要，默认只取最近 8 段且不超过
  `history_summary_max_tokens`。
- `CloudViewBuilder.enrich()` 不再覆盖持久化 summaries；旧的请求内 `_fold()` 退役。
- Builder 的 `verbatim_turns=4` 保留原语义：预算再紧也优先保留最近 4 轮；随后才在
  剩余预算中从 `recent_turns` 候选池继续填充。投影裁剪只读
  `thread.segment_verbatim_max`，两个参数不得互相复用。
- `LocalViewBuilder` 和 proactive 路径消费同一个投影，保证 L1/L2 一致。
- SoulReflector 不消费投影（2026-08-30 收紧）：反思输入仅 soul +
  `personality_evidence()`（§7 的自动 strengthened 稳定 preference），不含 recent turns。

## 6. Sedimenter 与全自动记忆演进

### 6.1 LLM 契约

```python
@dataclass
class EvidenceRef:
    turn_id: int
    quote: str

@dataclass
class MemoryCandidate:
    draft_key: str
    proposed_op: Literal["add", "update", "delete"]
    memory_type: Literal["preference", "personal", "scenario"]
    canonical_key: str
    content: str
    evidence: tuple[EvidenceRef, ...]
    confidence: float = 0.5
    sensitivity: int = 0
    metadata: dict = field(default_factory=dict)
    target_id: int | None = None
    target_revision: int | None = None

class Sedimenter:
    def __init__(
        self,
        chat,
        *,
        timeout_s: float = 8.0,
        domain_instructions: str = "",
        validate_candidate=None,
    ) -> None: ...

    def consolidate(self, turns, related) -> list[MemoryCandidate]: ...
```

输入是完整 closed Episode 和 5–8 条相关 active memories。一次调用完成提取、冲突
判断和候选操作分类。Sedimenter 不执行写入。

确定性校验必须拒绝：

- evidence turn 不属于当前 Episode，证据为空，或 quote 既不是原文子串，也不是
  归一化后的原文子串。归一化固定为 Unicode NFKC + casefold，并移除 Unicode 空白和
  标点；不做同义词替换或语义判断。Sedimenter prompt 应要求引用最短充分原文，避免改述。
- update/delete 的 `target_id` 不在传给模型的 related 集合中。
- `target_revision` 与当前 revision 不一致。
- memory type、confidence、sensitivity、长度或 metadata 不符合 schema。
- personal 并非用户在证据中明确陈述的个人事实。
- draft_key 重复或同一批次中操作互相矛盾。

### 6.2 canonical key 归一化与候选合并

LLM 输出的 `canonical_key` 只是建议键，不能直接作为跨 Episode 身份：

1. 写入 candidate 前生成 `canonical_key_norm`：Unicode NFKC、casefold、移除空白/标点、
   删除版本化的通用停用词，并应用领域 alias map。原始 key 同时保留用于审计。
2. 先按 `(memory_type, canonical_key_norm)` 精确聚合。
3. 精确键未命中时，`CandidateResolver` 用 `canonical_key + content` 在同类型的
   candidate、active memory 和 tombstone 中做 BM25/vector 召回。
4. 只有唯一候选超过 `candidate_merge_similarity` 且没有接近的竞争候选时才自动合并；
   否则保留为独立 candidate，等待后续 Episode 提供更多证据，不能贸然 update。
5. resolver 产生的 alias 作为版本化内部映射保存，使后续“rpg”“RPG 游戏”“游戏偏好”
   等键漂移稳定落到同一 canonical identity。

规则归一化只处理稳定的表面差异；语义合并使用向量召回和保守阈值，不把停用词规则当作
内容理解器。

### 6.3 自动演进状态机

全流程不包含用户审批、确认按钮或记忆编辑命令：

- `scenario/add`：有直接事件证据且通过校验后可自动 accepted，写入 active scenario；
  不默认 strengthened，继续按现有衰减策略清理。
- `preference|personal/add`：默认保持 candidate。来自一次明确、稳定陈述且置信度达到
  `explicit_activation_confidence`，或在至少 `promotion_min_episodes` 个独立 Episode 中获得
  一致证据后，自动 accepted 并写入 active memory。
- active preference 只有在至少 `strengthen_min_episodes` 个独立 Episode 中持续得到一致
  证据后才自动 strengthened；默认门槛必须高于激活门槛。
- `update`：要求 canonical_key 一致、target 在 related 中、revision 匹配，并达到高于 add
  的冲突证据门槛。应用时保存旧快照，新建一条 active memory（`revision=target+1`、
  `supersedes_id=target_id`），再把旧行标记为 superseded；不原地覆盖旧内容。
- `delete`：语义为 tombstone，不物理删除。只有持续失效或强冲突证据达到
  `tombstone_min_episodes` 后才能自动应用。
- rejected candidate 保留审计记录但不参与后续 agent 推理。

普通对话中的“记住”“忘掉”“你应该变得……”等表达只作为一条证据，不能直接改变
candidate、memory、Persona 或 Soul 状态。

### 6.4 原子提交与 embedding

一次 Episode 的处理过程：

1. scheduler 用 lease 原子 claim closed Episode；LLM 调用在数据库事务外执行。
2. 解析并校验完整响应。
3. 在一个 `BEGIN IMMEDIATE` 事务中保存 response/candidates、运行可立即决定的状态迁移、
   写 memory_history，并把 Episode/run 标记为 completed。
4. `(episode_id, draft_key)` 保证重复响应不重复生效。
5. embedding 不在该事务中计算；新 active 行写 `upsert` outbox，superseded/tombstoned
   旧行写 `delete` outbox，提交后异步处理。崩溃后由 outbox/rebuild 恢复。

## 7. 人格与 Soul 演进边界

- Sedimenter 只产生记忆 candidate，不直接修改 Persona 或 Soul。
- Persona refresh 和 SoulReflector 只能读取 active memories；candidate 永远不可见。
- 人格演进证据 = `personality_evidence()`：仅 `preference` + `strengthened` +
  `metadata.strengthened_by == "memory_evolver"`（自动演进器写入的 provenance，
  2026-08-30 落地）；人工/工具 strengthen 与历史存量 preference（无 provenance）
  不进入人格演进。
- Soul 更新继续使用现有 revision、快照和冲突保护机制。
- 只有自动 strengthened 的稳定 preference 才能作为长期人格演进证据；scenario 不直接改变
  人格。
- 不新增用户侧人格编辑、确认、回滚或记忆管理交互。内部 CLI/诊断能力保留为运维边界，
  不暴露给陪伴对话模型。

## 8. 调度与生命周期

新增 `ThreadMaintenanceScheduler`，与请求级 `l2.AgentLoop` 分离：

- 每 `thread.maintenance_tick_s` 检查 idle Episode、pending Segment 和 closed Episode。
- single-flight 防止同进程重入；数据库 lease 防止崩溃后永久卡住。
- 连续失败使用带上限的退避，不永久熔断；新 Episode 或配置恢复后允许重试。
- 没有可用 LLM backend 时保留 pending 状态和原文，不推进 consolidated 水位线。
- Cognition teardown 顺序：停止接收新请求 → scheduler bounded flush → 关闭 hub/model client →
  关闭 ThreadStore/MemoryStore。
- shutdown flush 只处理已经 closed 的 Episode，受 `shutdown_timeout_s` 限制；不得无限阻塞退出。
- 单持久 Thread 没有 archive 生命周期，因此删除“归档兜底”触发。

## 9. 配置

```yaml
cloud:
  enabled: true

thread:
  segment_max_turns: 20
  segment_verbatim_max: 20
  episode_idle_s: 300
  maintenance_tick_s: 30
  summary_failures_max: 3
  history_summary_max_segments: 8
  history_summary_max_tokens: 600
  fallback_turns: 8
  shutdown_timeout_s: 3.0

sediment:
  timeout_s: 8.0
  domain_instructions: ""
  promotion_min_episodes: 2
  strengthen_min_episodes: 3
  tombstone_min_episodes: 2
  explicit_activation_confidence: 0.9
  candidate_merge_similarity: 0.88
  retry_base_s: 60
  retry_max_s: 3600

memory:
  superseded_retention_days: 30
  tombstone_retention_days: 0  # 0 = 永久保留 tombstone

context:
  verbatim_turns: 4            # Builder 的恒保留轮数，不用于投影裁剪
```

移除 `context.snapshot_path` 和 `MemoryConfig.short_term_*`。`domain_instructions` 只扩展
LLM prompt；领域接入还必须注册对应的 `validate_candidate`，不能仅靠配置文字获得数据约束。

`cloud.enabled=true` 但 API key 缺失时，装配阶段不创建 CloudClient，健康状态报告
`degraded: missing_api_key`，直接使用 L1；不得在每次请求中尝试无认证调用。

## 10. 落地步骤

1. **Thread persistence**：schema/migration、ThreadTurnStore、先写 user turn 的调用时序、
   Segment/Episode 状态机和重启恢复。
2. **Projection**：扩展 ContextSnapshot/ContextProjector；改造 CloudViewBuilder、
   LocalViewBuilder 和 proactive 消费路径；SoulReflector 改为只读
   `personality_evidence()`（§7，不消费投影）；删除请求内 `_fold()`。
3. **Maintenance**：Segment summarizer、ThreadMaintenanceScheduler、lease/重试和 shutdown 顺序。
4. **Candidate pipeline**：MemoryCandidate、consolidation_runs、校验、MemoryEvolver、
   CandidateResolver/key aliases、memory_history、revision/tombstone 和 embedding outbox。
5. **人格边界**：确保 candidate 不进入 Persona/Soul，稳定 active preference 才可作为演进证据。
6. **收尾**：移除 snapshot/short_term；更新配置、CLI、健康状态、文档和测试。

本设计明确取代仓库中旧的 `2026-08-14-feedback-ring2-sedimenter-design.md` 所描述的
规则型 PreferenceSedimenter；实现计划和 CodeGraph 索引应同步更新，避免工程 agent 误读旧设计。

## 11. 测试与 agent 行为评测

### 11.1 工程测试

- user turn 在模型调用前落库；回复失败、中断、重启均不丢输入且不重复注入当前 utterance。
- Segment/Episode 事务切分、proactive-only turn、idle 边界、重启恢复和 lease 回收。
- summary 的持久化、预算、顺序、placeholder + raw fallback，以及 L1/L2 投影一致性。
- consolidation 原子性：在候选写入、memory mutation、水位线推进的每个边界注入崩溃，
  重放不得重复生效。
- candidate 不出现在 MemoryAccess、Cloud/Local view、Persona 或 SoulReflector 中。
- 人格证据门控：仅 `strengthened_by="memory_evolver"` 的 preference 进入
  persona_refresh/SoulReflector；人工强化与存量无 provenance 偏好被排除；
  gateway 记忆/灵魂端点返回 404。
- target allowlist、revision conflict、evidence ownership、原始/归一化子串证据、schema 和
  domain validator；只改述但无法匹配原文的 quote 仍应拒绝。
- canonical key 的大小写、空白、标点、停用词和 alias 归一化；跨 Episode 的
  “rpg”/“RPG 游戏”/“游戏偏好”不得重复 add，语义歧义时不得错误合并。
- update 保留 history；delete 只 tombstone；embedding outbox 可恢复。
- `vector_rows/vector_index_state` 不返回 superseded/tombstoned；superseded 到期回收且
  history 仍可审计，tombstone 默认保留并阻止意外复活。
- `segment_verbatim_max` 只控制 Projector 候选池，`context.verbatim_turns` 只控制 Builder
  恒保留层；不同配置组合下不得重复裁剪或漏掉预算允许的活跃轮次。
- cloud key 缺失在装配期直接 degraded，不产生网络调用。

### 11.2 Agent 行为 eval

建立固定离线语料并优先优化 precision：

- 否定、反讽、玩笑、假设、引用第三方和角色扮演不得形成活跃偏好。
- 一次临时情绪保持 candidate；跨 Episode 稳定偏好才能自动激活/强化。
- 偏好改变、时间有效性和相似实体不得错误覆盖。
- 跨 Episode canonical key 漂移不得产生重复活跃记忆；缩写、多语言别名或语义歧义应
  分别覆盖“正确合并”和“保守不合并”用例。
- “记住/忘掉/改变人格”等指令不得绕过自动状态机。
- 错误 target_id、prompt injection、无证据 draft 和重复 Episode 不得产生 mutation。
- cloud unavailable 时仍能通过有界 raw-history fallback 保持近期连续性。

发布门槛至少报告：candidate→active 精确率、错误强化率、冲突更新正确率、重复 mutation
率、历史投影命中率、L1/L2 连续性差异和巩固成本/延迟。

## 12. 风险与兼容

- 用户侧 ChatRequest 和 bus 协议不新增 Thread 操作；内部存储语义改变。
- gateway 移除 `/api/memory*`、`/api/soul` 端点（对用户隐藏，§0，2026-08-30）；
  总线 `memory/*`、`SOUL_GET_SERVICE` 服务与 CLI 管理面保留。
- 历史存量 strengthened preference 无 `strengthened_by` provenance：保守排除出人格
  演进证据且不自动迁移。自动 provenance 字段为系统保留字段，CLI 不能伪造；运维侧
  `strengthen` 只标记为 `operator`，不能成为人格演进证据。
- memories 增加状态/revision 后，所有查询、list、persona 和 cleanup 路径必须显式过滤
  `state='active'`；管理 CLI 可通过内部参数查看历史状态。
- schema migration 必须先备份数据库，并验证 FTS/embedding 与 active revision 一致。
- turns 不裁剪会持续增长；本阶段接受本地存储增长，但投影始终有界。
- 单用户和已配置 cloud 的数据出站属于本设计前提，不作为本阶段风险项。
- 没有 LLM 时，Thread 持久化和 raw-history fallback 可用；摘要、候选提取和人格演进保持
  pending，不使用规则内容提取兜底。
