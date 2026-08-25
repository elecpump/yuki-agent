# 本地模型管理改进方案

## 总体目标

把当前分散在各模型类中的“加载、健康、缓存、清理”逻辑收拢到一个 **中央 ModelRegistry**，同时补齐 **GPU 显存管理、优先级调度、统一健康聚合、显式清理、热重载、配置预检** 能力。

---

## 1. 引入 ModelRegistry

### 核心职责

- 统一管理所有本地模型的注册、加载、卸载、健康查询。
- 维护模型状态机：

```
NOT_LOADED → LOADING → LOADED
                    ↘ DEGRADED
                    ↘ ERROR → NOT_LOADED
```

### 建议 API

```python
class ModelRegistry:
    def register(self, spec: ModelSpec) -> None: ...
    def load(self, model_id: str) -> None: ...
    def unload(self, model_id: str) -> None: ...
    def reload(self, model_id: str) -> None: ...
    def get_loaded_models(self) -> list[str]: ...
    def get_model_health(self, model_id: str) -> dict: ...
    def get_all_models_health(self) -> dict: ...
    def get_overall_status(self) -> dict: ...
    def shutdown(self) -> None: ...
```

### `ModelSpec` 设计

```python
@dataclass
class ModelSpec:
    name: str
    loader: Callable[[], Any]
    unloader: Callable[[Any], None]
    health_check: Callable[[], dict]
    priority: int          # 数值越小优先级越高
    dependencies: list[str]
    vram_estimate_gb: float = 0.0
    allow_unload: bool = True
```

---

## 2. GPU 显存管理与自动降级

### 新增 `GpuMemoryMonitor`

- 启动时获取总显存、已用显存。
- 周期性调用：

```python
torch.cuda.memory_stats()
torch.cuda.mem_get_info()
```

- 维护每个模型的显存占用估算。
- 当显存低于阈值时，触发降级策略。

### 降级策略

1. 找出当前 `LOADED` 且 `priority` 最低、且不处于推理中的模型。
2. 自动调用 `unload()`。
3. 释放显存并执行 `torch.cuda.empty_cache()`。
4. 如果 VLM 被卸载，则降级为纯文本模式；如果 STT 被卸载，则暂时关闭语音识别。

### 统一设备解析

- 将 `VLM.device_map="auto"`、`LocalChatModel.device_map="auto"`、`STT/VAD._resolve_device()` 统一收敛到 Registry 的设备分配逻辑，避免多个模型各自抢占 GPU。

---

## 3. 显式依赖图

### 注册依赖关系

```python
registry.register(ModelSpec(
    name="vlm",
    loader=...,
    dependencies=[],
    priority=2,
))

registry.register(ModelSpec(
    name="stt",
    loader=...,
    dependencies=[],
    priority=1,
))

registry.register(ModelSpec(
    name="vision_screen",
    loader=...,
    dependencies=["frame_client", "vlm"],
    priority=3,
))
```

### 自动加载/卸载顺序

- 加载时按拓扑序：先加载依赖，再加载依赖方。
- 卸载时按逆拓扑序：先卸载依赖方，再卸载被依赖方。
- 这样 `VisionScreenAdapter` 对 `frame_client` 和 `vlm` 的依赖就从隐式构造变为显式声明。

---

## 4. 统一健康聚合与可观测性

### 模型级健康

每个模型 `health_check()` 至少返回：

```json
{
  "loaded": true,
  "degraded": false,
  "latency_p50_ms": 120.0,
  "latency_p95_ms": 300.0,
  "success_count": 100,
  "failure_count": 2,
  "last_error": ""
}
```

### 聚合接口

- 在现有 `HealthReporter` 基础上扩展，而不是另起一套。
- `CognitionAgent.health_components()` 中增加：

```python
"models": self._health_models
```

- `_health_models()` 调用 `registry.get_all_models_health()`。
- 任一关键模型 degraded 时，应影响整体 `healthy` 字段，而不是像现在 `_health_vlm` / `_health_stt` 永远返回 `ok=True`。

### 暴露端点

- 继续使用 `health/cognition`，但返回结构增加：

```json
{
  "models": {
    "vlm": {...},
    "stt": {...},
    "vad": {...},
    "local_chat": {...}
  },
  "overall_model_status": "healthy | degraded | unhealthy"
}
```

---

## 5. 显式资源清理与热重载

### 模型类增加统一接口

```python
class BaseLocalModel:
    def load(self) -> None: ...
    def unload(self) -> None: ...
    def reload(self) -> None: ...
    def health(self) -> dict: ...
```

- `VisualUnderstander`
- `SpeechRecognizer`
- `FsmnVadBackend`
- `LocalChatModel`

都实现这些接口。

### `unload()` 实现要点

- 删除模型引用。
- 执行 `torch.cuda.empty_cache()`。
- 将 `_loaded = False`。
- 重置 `LoadGate` 状态，允许下次重新加载。

### 接入现有生命周期

- `CognitionAgent.teardown()` 中调用：

```python
self._model_registry.shutdown()
```

- 可选接入 `ShutdownManager.register_cleanup(...)`，让清理支持优先级。

### 热重载

- `reload()` = `unload()` + `load()`。
- 支持运行时切换模型版本：先加载新版本，成功后再卸载旧版本，失败则回滚。

---

## 6. 配置验证与预检

### 模型特定校验

在 Pydantic 模型中增加 validator：

```python
@field_validator("device")
def validate_device(cls, v):
    allowed = {"auto", "cpu", "cuda", "cuda:0"}
    if v not in allowed:
        raise ValueError("unsupported device")
    return v
```

### 预检机制

- Registry 提供 `preflight()`。
- 启动时检查：
  - 模型 ID 是否能从本地缓存加载。
  - 本地模型文件是否存在。
  - 显存是否足够。
  - 模型与当前 Transformers / FunASR 版本是否兼容。
- 预检失败时给出明确错误，而不是异步 warmup 失败后默默降级。

---

## 7. 统一缓存管理

### 新增 `ModelCacheManager`

```python
class ModelCacheManager:
    def __init__(self, max_entries=256, max_memory_mb=512): ...
    def get(self, model_name: str, key: str): ...
    def put(self, model_name: str, key: str, value): ...
    def clear(self, model_name: str | None = None): ...
```

### 接入模型

- VLM 的 `ContextCache` 迁移到统一缓存。
- STT 可增加短时识别结果缓存，避免相同音频重复识别。
- LocalChat 可增加 prompt/embedding 级缓存。
- 统一支持 LRU、TTL、内存上限。

---

## 8. 错误关联与共享上下文

### 新增 `ModelErrorContext`

- 在 Registry 中维护共享错误上下文：

```python
class ModelErrorContext:
    def record(self, model: str, error: Exception, correlation_id: str): ...
    def recent_incidents(self) -> list[dict]: ...
```

### 使用场景

- 当检测到 `torch.cuda.OutOfMemoryError` 时，记录一条 `"gpu_oom"` 事件。
- 所有在同一时间窗口内失败的模型都可以关联到该 OOM 事件。
- 日志中增加 `correlation_id`，便于排查多模型同时失败是否同因。

---

## 实施阶段建议

| 阶段 | 内容 | 优先级 |
|---|---|---|
| Phase 1 | ModelRegistry + 模型生命周期 + shutdown | P0 |
| Phase 2 | GPU 显存监控 + 优先级卸载降级 | P0 |
| Phase 3 | 健康聚合 + 延迟/成功率指标 | P1 |
| Phase 4 | 配置预检 + 统一缓存 | P1 |
| Phase 5 | 热重载 + 版本切换 | P2 |
| Phase 6 | 错误关联与共享上下文 | P2 |

---

## 涉及文件

```text
新增：
  src/yuki/cognition/model_registry.py
  src/yuki/cognition/gpu_monitor.py
  src/yuki/cognition/model_cache.py
  src/yuki/cognition/error_context.py

修改：
  src/yuki/cognition/assembly.py        # 接入 ModelRegistry
  src/yuki/cognition/agent.py           # teardown 调用 registry.shutdown()
  src/yuki/cognition/vlm.py             # 实现 load/unload/reload
  src/yuki/cognition/stt.py             # 实现 load/unload/reload
  src/yuki/cognition/vad.py             # 实现 load/unload/reload
  src/yuki/cognition/brain/local/model.py  # 实现 load/unload/reload
  src/yuki/health.py                    # 扩展模型级健康聚合
  src/yuki/config.py                    # 增加模型特定校验
```

---

## 最终效果

改造完成后，系统将具备：

- 一个统一的模型注册中心，能查询“当前加载了哪些模型”。
- 显存压力下的自动降级，避免多模型同时 OOM。
- 按优先级和依赖关系自动加载/卸载。
- 统一健康端点，能反映整体模型健康状态。
- 显式清理，不再依赖 Python GC 回收 GPU 模型。
- 支持热重载和运行时切换模型版本。
- 统一的缓存策略和错误关联能力。
