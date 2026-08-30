# Yuki Context

Yuki 是单机娱乐陪伴型 agent。本术语表定义记忆系统与对话流组织（Thread + 用户级记忆）的规范语言；记忆类型的持久化/检索/隐私细节见 `docs/superpowers/specs/`。

## Language

**Thread**:
对话流的内部组织单元，由 agent 维护、对用户隐藏且不可主动操作：单持久 Thread 内按对话段/段落组织，turns 全量持久化（SQLite，无 TTL）；不承载关系状态（关系状态在用户级记忆）。
_Avoid_: session（现有 `session_id` 仅为字符串 ID，无生命周期与持久化语义）

**Scenario（场景记忆）**:
从对话中提取的关键事件，用户级、带时间戳、跨 Thread 共享，metadata 承载事件信息；可随衰减清理。
_Avoid_: episodic memory

**Preference（偏好记忆）**:
用户的偏好、习惯、事实，用户级、长期有效；新提取结果默认进入 candidate，达到自动晋升门槛后才成为 active memory，用户不直接参与确认或编辑。
_Avoid_: semantic memory

**Personal（个人事实记忆）**:
用户在对话中明确陈述的个人事实；必须具有可定位到用户原话的证据，新提取结果默认进入
candidate，并按自动晋升门槛演进。不得从 agent 转述或推测中生成。

**Strengthened（强化记忆）**:
置位后衰减权重恒 1.0 且豁免自动清理。长期人格证据还必须带有自动演进器生成的
provenance；CLI/运维强化只标记为 `operator`，不能影响 Persona 或 Soul。candidate 永远
不参与 agent 推理、Persona 或 Soul 演进。

**Sedimenter（记忆巩固）**:
只通过 LLM 从对话轮次提取记忆草稿（偏好/事件/事实），无规则兜底；LLM 不可用或失败时跳过巩固并保留轮次原文，待后续重放。

**Episode（对话段）**:
用户主动发起、到闲置超过阈值（默认 5min）为止的连续轮次序列；记忆巩固只处理已结束的完整对话段，未结束的对话段不提取。

**Segment（段落）**:
单 Thread 内的上下文压缩单元，按长度切分（默认 20 轮）：活跃段承载 verbatim 轮次，历史段保留 summary + 全量 turns 原文；段 summary 允许失败占位降级（与巩固的 LLM-only 刻意区分）。
