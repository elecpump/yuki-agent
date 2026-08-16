# Yuki MemoryManager 记忆系统设计

> 日期：2026-08-14
> 状态：已实现；2026-08-17 增补三档隐私策略与 purpose 访问层
> 范围：记忆系统（MemoryManager），独立模块 + CLI + 总线服务 + 隐私访问边界

## 1. 背景与目标

现有实现中记忆系统为零（无 memory 模块）。设计文档 `2026-08-10-yuki-agent-design.md` §5 定义了记忆系统但未落地，且其"三类记忆 + 向量库"在本次会话经确认调整：

- **存储**：纯 SQLite + FTS5（零新增依赖，符合当前零模型运行时哲学），向量检索留待后续。
- **记忆类型**：从"偏好/对话/阅读"三类扩展为**认知架构分层**的五类：短期（工作记忆）、偏好、个人信息、场景、反思。
- **加密**：暂不加密（本地单机、pre-1.0），文档注明为后续项。
- **集成面**：独立模块 + CLI（离线直连 DB）+ `memory/*` REQ/REP 总线服务（运行时供未来 Brain 调用）。

**范围外**：Brain/主动评论、反馈自进化闭环、对话历史（尚无对话系统写入方）、反思生成逻辑（仅存结构）、向量检索。

## 2. 架构与文件布局

新增 `src/yuki/memory/` 包，四层职责单一：

| 模块 | 职责 |
|---|---|
| `store.py` | `MemoryStore`：SQLite 持久化 + FTS5 索引 + 触发器同步；单连接 + `threading.Lock` 防并发 |
| `manager.py` | `MemoryManager`：门面。类型感知 API、衰减加权检索、清理、短期记忆（进程内 TTL 队列） |
| `privacy.py` | `MemoryPrivacyPolicy` + `MemoryAccess`：所有模型/工具读取必须声明 purpose，默认 fail-closed |
| `service.py` | 总线接线：`memory/*` REQ/REP 处理器注册函数 |
| `cli.py` | 命令行管理工具（`python -m yuki.memory`），**直连 DB 文件**（离线可用，不依赖总线） |

- `__main__.py`：CLI 入口。
- `Reflector`：反思生成**只定义接口**，本次实现抛 `NotImplementedError`，注释标注 LLM 接入点。
- 短期记忆**仅驻内存**（deque + TTL + 容量上限），不落盘。

### 2.1 进程归属

- 总线服务注册在 `CognitionAgent.setup()`（记忆属认知层，与设计文档一致）。
- CLI 为独立入口，直接打开 `db_path`，不经过总线。

## 3. 数据模型

### 3.1 `memories` 表（四类持久化记忆统一 schema）

```
id            INTEGER PK
memory_type   TEXT  CHECK IN ('preference','personal','scenario','reflection')
content       TEXT  主文本
confidence    REAL  0..1 默认 0.5
sensitivity   INTEGER 0=普通 / 1=私密 / 2=高敏  默认 0
source        TEXT  ('user','cli','pipeline','feedback','brain')
metadata      TEXT  JSON blob（topic/url/scenario_id/session_id/key_points...）
created_at    REAL
last_access   REAL  初值=created_at（设计文档 §5.2 v3 修正）
access_count  INTEGER
strengthened  INTEGER 0/1 手动强化标记
```

### 3.2 类型语义

| 类型 | 定位 | 存储 | 衰减 |
|---|---|---|---|
| `preference` | 用户偏好（显式/隐式） | 持久化 + FTS | 有 |
| `personal` | 个人信息（姓名/职业等），高敏倾向 | 持久化 + FTS | 有，但**排除自动清理** |
| `scenario` | 场景/情境绑定记忆（"用户在读某主题文章"） | 持久化 + FTS | 有（metadata 承载 topic/url 等） |
| `reflection` | 高层洞察，本次**只存结构不生成** | 持久化 + FTS（metadata.source_refs 引用来源记忆 id 列表） | 有 |
| `short_term` | 工作记忆（当前情境/最近上下文） | 仅内存 | TTL 逐出 |

### 3.3 敏感度语义

| sensitivity | 含义 | 自动暴露策略 |
|---|---|---|
| `0` 普通 | 可帮助理解用户和任务 | 可进入本地模型、云端 L2、云端 persona 精修、LLM tool result |
| `1` 私密 | 只应留在本机的个性化信息 | 可进入本地模型上下文；不得进入云端 L2、云端 persona 精修或云端工具结果 |
| `2` 高敏 | 密码、身份、财务、医疗等强敏信息 | 不进入任何自动模型上下文；仅用户显式查看/管理 |

`source` 不决定敏感度：显式用户偏好如果内容非敏感，应写为 `0` 以便进入 persona；只有命中敏感规则或用户/工具显式标记时才升为 `1/2`。

### 3.4 FTS5

- 虚拟表 `memories_fts`，**trigram tokenizer**（SQLite ≥3.34，支持中文子串匹配；本机验证 SQLite 3.53.1 可用），外部内容表（`content='memories'`），触发器同步 `memories.content` 的插入/更新/删除。
- **<3 字符查询兜底（已验证必须）**：trigram 不支持少于 3 字符的子串查询，会漏掉双字中文词（"计算""股票"）与英文短词。因此 `search` 对**最短分词 < 3 字符**的查询走 `content LIKE '%'||?||'%'` 兜底（本地小型库量级下性能可接受），≥3 字符走 FTS + bm25。两种路径均带 `memory_type`/`sensitivity` 过滤。

## 4. 检索与衰减

### 4.1 衰减权重（设计文档 §5.2）

```
decay_weight = 1.0 if strengthened else base * exp(-λ * days_since_last_access)
```

- 默认 `base=1.0`、`λ=0.1`：7 天 ≈0.5、30 天 ≈0.05。
- 新记忆 `last_access = created_at`，首次被访问后切换为真实访问时间。

### 4.2 检索打分

```
score = FTS rank（归一化） × decay_weight
```

- FTS 路径：`rank = 1/(1+|bm25|)`；LIKE 兜底路径 `rank = 1.0`（仅按衰减与 recency 排序）。
- `query` 命中即 `touch`（更新 `last_access`/`access_count`），实现"首次被访问后切换为真实访问时间"。

- 底层 `MemoryManager` 仍支持按 `min_sensitivity` 过滤（默认 0），只作为存储查询条件。
- 返回 `top_k` 条。
- 模型相关读取不得直接使用裸 `MemoryManager.query/list/get` 结果，必须通过 `MemoryAccess(..., purpose=...)` 二次授权。

### 4.3 隐私访问 purpose

| Purpose | 允许敏感度 | 用途 |
|---|---|---|
| `user_explicit_view` | `0/1/2` | CLI 或用户显式 `memory/list`、`memory/get` 管理 |
| `local_model_context` | `0/1` | 本地模型/本机检索上下文 |
| `cloud_model_context` | `0` | L2 云端 prompt 长期记忆 |
| `persona_refine_cloud` | `0` | 规则 persona 组装和可选云端精修输入 |
| `llm_tool_query_result` | `0` | 云端 tool_call 的 `memory.query/list/get` 回填 |

### 4.4 清理

- `cleanup()`：删除 `decay_weight < threshold`（默认 0.02）且**非 strengthened** 的记忆。
- **`personal` 类型排除在自动清理外**（须用户显式删除）。

### 4.5 强化

- `strengthen(id)`：置位 `strengthened=1` + 重置 `last_access`，此后衰减权重恒 1.0。

## 5. 集成面

### 5.1 CLI（`python -m yuki.memory`）

```
list [--type T] [--min-sensitivity N]     查看
query TEXT [--type T] [--top-k N]         检索
add --type T --content C [--confidence] [--sensitivity] [--source] [--metadata K=V,...]
get ID / delete ID / strengthen ID
wipe                                      一键全清（物理删除 + FTS 重建）
short-term                                查看当前工作记忆
```

- `wipe` 需确认提示（避免误删）。
- 退出码：成功 0，用法错误 2（argparse 默认），错误 1。
- CLI 是**全权管理面**：直连 DB 的 `query` 返回 `0/1/2`（含高敏），与总线 `memory/query`（`local_model_context`，过滤 `2`）语义不同——CLI 是用户本机管理工具，不把结果喂给任何模型。

### 5.2 总线服务

在 `CognitionAgent.setup()` 注册，载荷用现有 `payloads.py` TypedDict 风格（新增 `payloads.py` 中的 memory 相关 TypedDict）：

| 服务 | 载荷 → 返回 |
|---|---|
| `memory/write` | `{memory_type, content, confidence?, sensitivity?, source?, metadata?}` → `{id}` |
| `memory/query` | `{text, type?, top_k?, min_sensitivity?}` → `{results: [{id, content, score, ...}]}` |
| `memory/list` | `{type?, min_sensitivity?}` → `{results: [...]}` |
| `memory/get` | `{id}` → `{memory}` 或错误 |
| `memory/delete` | `{id}` → `{deleted: bool}` |
| `memory/strengthen` | `{id}` → `{ok: bool}` |
| `memory/wipe` | `{}` → `{deleted_count}` |

隐私语义：

- `memory/query` 作为本机检索服务，走 `local_model_context`，允许 `0/1`、过滤 `2`。
- `memory/list` / `memory/get` 作为用户显式查看路径，走 `user_explicit_view`，允许 `0/1/2`。
- LLM 函数工具中的 `memory.query/list/get` 走 `llm_tool_query_result`，只允许 `0`。

### 5.3 配置

`Config` 新增 `memory` 节（env `YUKI_MEMORY_*`）：

```yaml
memory:
  db_path: "data/yuki.db"
  decay_base: 1.0
  decay_lambda: 0.1
  decay_threshold: 0.02
  short_term_ttl_s: 1800
  short_term_capacity: 50
```

`db_path` 相对路径相对 CWD 解析；父目录不存在时自动创建。

### 5.4 健康检查

`CognitionAgent.health_components()` 新增 `memory` 组件检查（DB 可达：执行 `SELECT 1`）。

## 6. 错误处理与测试

### 6.1 错误处理

- 并发：`MemoryStore` 单连接 + 锁；总线 handler 在 dealer 线程调用，统一走 REQ/REP 契约。
- DB 损坏/不可写：store 抛 `MemoryError`（自定义异常），总线 handler 转 `build_response_error`；CLI 打印错误退出 1。
- `wipe` 实现为：删除 `memories` 全部行（DELETE 触发器同步删 FTS 行），再对空表执行 FTS 完整性重建（`INSERT INTO memories_fts(memories_fts) VALUES('rebuild')`），确保索引物理一致、无残留。

### 6.2 测试

- `tests/test_memory_store.py`：CRUD、FTS 中文检索、触发器同步、衰减评分（新鲜 > 旧）、strengthen 重置、cleanup 边界（personal 与 strengthened 豁免）、sensitivity 过滤。
- `tests/test_memory_manager.py`：短期记忆 TTL/容量逐出、门面转发。
- `tests/test_memory_cli.py`：各子命令（临时目录直连 DB）、wipe 确认提示、退出码。
- `tests/test_memory_service.py`：各总线 handler（经 `tests/fakes.py` FakeBus）。
- `tests/test_memory_privacy.py`：三档敏感度 purpose 策略和访问层过滤。
- `tests/functions/test_memory_tools.py`：LLM memory tools 不返回私密/高敏记忆。
- e2e 行为等价：现有断言不变（新增只注册服务，不改变数据流）。

## 7. 风险与兼容

- 零协议变更：总线新增 `memory/*` 服务名，不触碰现有主题/服务。
- 零运行时行为变化：只在 cognition 启动时多注册一组 REQ/REP handler。
- 新增依赖：无（stdlib `sqlite3`）。
- 不加密为**已知限制**：明文落盘，仅限本机、pre-1.0，后续可平滑加 sqlcipher 或字段混淆（表结构不变）。
- 反思生成、向量检索为**明确的后续项**（接口占位 + TODO 注释）；云端记忆出站过滤已由 `MemoryAccess` purpose 策略实现。
- **写入信任为已知限制**：`memory/write` 工具/总线把调用方上报的 `sensitivity` 原样透传，云端 LLM 可写入实际敏感但标 `0` 的记忆并回流云端；仅 sedimenter 写入侧做内容级打标。后续可在 write 路径统一加内容扫描。

## 8. 关键决策记录（ADR 摘要）

| 决策 | 理由 |
|---|---|
| 纯 SQLite + FTS5（trigram） | 零新依赖；中文子串匹配可用；向量后续可插 |
| 五类认知架构分层 | 用户确认；短期=工作记忆、偏好/个人=核心长期、场景=情景、反思=高层结构 |
| 反思只存结构不生成 | 无 LLM；接口占位，LLM 接入后替换 |
| 暂不加密 | 本地单机、pre-1.0，避免 sqlcipher 重依赖 |
| CLI 直连 DB，总线服务走运行时 | CLI 离线可用作管理工具；总线面向未来 Brain |
| `last_access` 初值 = `created_at` | 设计文档 §5.2 v3 修正，避免新建记忆永远高权重 |
| `personal` 排除自动清理 | 个人信息不可被衰减误删，须用户显式删除 |
| 模型读取必须声明 purpose | 忘写过滤时更容易 fail-closed，而不是把私密/高敏数据当普通记忆外发 |
