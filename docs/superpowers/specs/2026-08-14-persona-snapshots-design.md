# Yuki 反馈闭环环3: 人格快照 设计

> 日期：2026-08-14
> 状态：已实现；2026-08-17 增补 persona 记忆隐私边界
> 范围：环3 人格快照——规则组装 persona 提示（LLM 精修可选）、生成即版本、cap/锁/跳过、回滚/导出；CloudBridge 用 active 快照；偏好读取走云端安全 purpose

## 1. 背景与目标

实现设计文档 `2026-08-10-yuki-agent-design.md` §6.3 的**环3 人格演化**：把基础人格提示 + 沉淀偏好 + soul 参数确定性组装成 persona 提示（默认路径），可选 LLM 精修；每次生成即存为版本快照（cap/锁/跳过相同控制增长），支持查看差异/一键回滚/导入导出/重置。soul 为环3 的快照载体（此前只存参数，本轮并入 persona 提示）。

**已确认决策**：
- **规则组装为主（默认路径）**：`PersonaPrompt = config.persona.prompt（基础人格描述）+ format_preferences（cloud-safe preference 记忆）+ format_soul_params（soul 参数说明）`。完全确定、可单测；因为快照会被 CloudBridge 复用，偏好读取始终按云端安全策略过滤。
- **LLM 精修为可选开关（默认关）**：`persona.enable_llm_refine: false`；开且 L2 可用时把规则结果喂 L2 生成更自然文本；超时/失败回退规则结果；结果缓存。
- **生成即版本**：会话初始化/偏好更新后生成 active persona；与当前 active 完全相同则**跳过建版**；否则存新版本并设 active。
- **cap + 锁**：`persona.max_versions=50`，超出删最旧**非锁定**版本；CLI 可锁定重要版本（豁免清理）。
- **全量存储**（不采用增量 diff 链）：提示词 KB 级、cap 已限增长，diff 链与"删最旧"冲突且复杂度不值。
- **生成时机**：会话初始化/偏好更新后生成一次，缓存；不每轮重建。

**范围外**：LLM 精修的自动质量评估、多 persona 并行、persona 提示的 A/B 测试、快照的跨设备同步。

## 2. 架构与文件布局

```
src/yuki/cognition/brain/
  persona.py      — PersonaGenerator（规则组装 + LLM 精修可选开关）
  snapshots.py    — PersonaStore（版本历史：建版/跳过/cap/锁/回滚/reset/diff/导出导入）
  cli 扩展或复用  — persona 子命令（查看/发布/锁定/回滚/reset/导出/导入）
src/yuki/config.py — persona: { prompt, max_versions, enable_llm_refine, snapshots_path }
CloudBridge 用 active persona 提示（替代硬编码 DEFAULT_PERSONA_PROMPT）
```

- `persona.py` 纯生成；`snapshots.py` 纯存储；`CloudBridge` 消费 active。各组件独立可测。

## 3. PersonaGenerator（persona.py）

```python
def generate(persona_name: str, preferences: list[dict], soul_params: dict,
             base_prompt: str | None = None) -> str: ...
def format_preferences(preferences: list[dict]) -> str: ...   # 偏好段落模板
def format_soul_params(params: dict) -> str: ...              # 参数说明模板
```

- 组装顺序：`base_prompt`（config.persona.prompt，含 `{persona}` 占位注入 persona_name）→ `format_preferences`（preference 记忆，"用户偏好：- 语气：温柔…"）→ `format_soul_params`（soul 参数，"你的表达应偏向…"）。
- 偏好/参数为空 → 省略对应段（或默认描述）。
- **LLM 精修（可选）**：`enable_llm_refine` 开且 L2 可用时，把已过滤的规则结果作为输入调 `CloudBridge` 生成更自然人格文本；超时/失败回退规则结果；结果缓存（会话级，键=规则结果哈希）。

## 4. PersonaStore（snapshots.py）

```python
@dataclass(frozen=True)
class PersonaSnapshot:
    version: int
    persona_prompt: str
    params: dict
    created_at: float
    locked: bool = False

class PersonaStore:
    def __init__(self, path, *, max_versions: int = 50) -> None: ...
    def active(self) -> PersonaSnapshot | None: ...
    def save(self, persona_prompt: str, params: dict) -> PersonaSnapshot | None: ...
        # 生成即版本：与 active 完全相同 → None（跳过）；否则存新版本并设 active；超 cap 删最旧非锁定
    def list_versions(self) -> list[PersonaSnapshot]: ...
    def rollback(self, version: int) -> None: ...      # 设 active（§6.4）
    def lock(self, version: int) -> None: ...
    def reset(self) -> None: ...                       # 回基础快照（§6.4 一键重置）
    def diff(self, v1: int, v2: int) -> str: ...       # 两版提示差异（difflib）
    def export(self, version: int) -> dict: ...        # 导出 json
    def import_snapshot(self, data: dict) -> None: ...
```

- **存储**：全量快照 json（每版 prompt+params+locked+created_at），KB 级。
- **cap 清理**：`len(versions) > max_versions` 时删最旧**非锁定**版本；**v1（基础快照）永远不参与自动清理**。
- **active 指针**：rollback/reset 修改。
- **偏好来源与触发**：生成时由调用方（agent/hub）通过 `MemoryAccess(..., purpose=MemoryPurpose.PERSONA_REFINE_CLOUD)` 读取 preference，只允许 `sensitivity=0`。普通显式偏好（如"请回复简短一些"）应写为 `0` 并进入 persona；`sensitivity=1` 私密偏好可服务本地未来路径，但不得进入持久 persona 快照或 L2 精修输入；`sensitivity=2` 高敏不进入任何自动 persona 路径。触发时机 = 会话初始化 + 环2 沉淀更新后。

## 5. 消费

- `CloudBridge` 构造时用 **active persona 快照的 prompt** 作为系统提示（替代硬编码 `DEFAULT_PERSONA_PROMPT`）；无快照时回退 `config.persona.prompt`。
- soul 仍存实时参数（环1 契约）；快照 `params` 是生成时的参数快照。

## 6. 配置

```yaml
persona:
  prompt: "你是{persona},一个温柔的中文语音陪伴 agent。回复简短自然(1-3 句),贴合陪伴场景。不替用户操作系统或浏览器。用户提到自伤/自杀等危机时,优先表达关怀并建议求助。可以用工具查询记忆,但不要捏造记忆内容。"
  max_versions: 50
  enable_llm_refine: false
  snapshots_path: "data/persona_snapshots.json"
```

env：`YUKI_PERSONA_PROMPT` / `YUKI_PERSONA_MAX_VERSIONS` / `YUKI_PERSONA_ENABLE_LLM_REFINE` / `YUKI_PERSONA_SNAPSHOTS_PATH`。

## 7. 测试

- `test_persona.py`：规则组装（基础+偏好+参数）、偏好/参数为空省略、`{persona}` 注入、format_preferences/format_soul_params 模板、LLM 精修开/关/失败回退/缓存。
- `test_snapshots.py`：建版（active 更新）、跳过相同（生成与 active 一致 → None）、cap 清理（删最旧非锁定、锁定豁免）、rollback/reset、lock、diff、export/import 往返、损坏文件容错。
- `test_bridge.py`：CloudBridge 用 active 快照 prompt、无快照回退 config.prompt。
- `test_cognition.py`：agent 装配 PersonaStore/Generator。
- `test_cognition.py`：云端 persona refine 输入只包含公开 preference。
- `test_cognition.py`：普通显式沉淀偏好进入 persona refine；高敏显式沉淀偏好被排除。
- e2e 不变（默认无偏好变化 → 跳过相同 → 不产生版本文件）。

## 8. 风险与兼容

- 零协议变更（REPLY 主题/载荷不变）；零新依赖（difflib 等 stdlib）。
- `DEFAULT_PERSONA_PROMPT` 从 CloudBridge 代码常量迁至 `config.persona.prompt`——默认值逐字迁移，无行为变化；CloudBridge 用 active 快照或回退 config 值。
- 快照文件损坏 → 回退 config 基础提示，不崩溃。
- **已知限制**：LLM 精修质量未自动评估；版本全量存储（KB 级 × cap=50）；偏好/参数变化才产生新版本（跳过相同）。
- **后续接入点**（明确范围外）：LLM 精修质量评估/个性化按用户、多 persona 并行、跨设备同步。

## 9. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 规则组装为主 + LLM 精修可选 | 确定、可测、无云依赖；L2 稳定后可开 |
| 生成即版本 + 跳过相同 | 版本史完整；相同不产生噪音版本 |
| cap 50 + 锁定豁免 | 版本数有界；重要版本不丢 |
| 全量存储（非增量 diff 链） | 提示词 KB 级、cap 已限增长；diff 链与删最旧冲突 |
| 会话初始化/偏好更新后生成一次并缓存 | 避免每轮重建的不一致与浪费 |
| CloudBridge 用 active 快照 | 人格快照真正生效；无快照回退基础 |
| persona 快照只使用公开偏好 | 快照会成为云端 system prompt，不能把 local-only 私密偏好间接外发 |
