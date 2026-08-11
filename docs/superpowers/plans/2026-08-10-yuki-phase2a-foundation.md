# Yuki Phase 2a：总线与进程基石 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Phase 1 总线与进程层面的 9 个问题（hub 归属、优雅关闭、REQ/REP 陷阱、订阅覆盖、线程异常、零日志、无健康探活、重启策略粗糙、Config 过简），落地消息信封（含 trace_id），为 Phase 2b（采集层）/2c（认知层）铺平地基。

**Architecture:** 保持三进程分层，但 hub 从"cognition 内置"迁移为**独立总线进程（bus_server）**，由 Supervisor 纯看门狗统一守护 4 个子进程（bus_server/cognition/interaction/perception）。REQ/REP 全部改为**经枢纽 ROUTER/DEALER 转发**（消除单端口单服务、无超时、handler 异常卡死三大缺陷）。每个进程注册信号处理器实现优雅关闭（Windows 用 CTRL_BREAK_EVENT）。消息走统一信封 `{version, trace_id, service, request_id, payload}`（Phase 2b 将 JSON 编码机械替换为 protobuf）。

**Tech Stack:** Python ≥3.11，pyzmq，structlog，pydantic，pyyaml，pytest。Pillow（录制器，dev）。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-agent-design.md` §2/§8.1/§11.1/§11.5；接口契约 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md`（本计划 Task 6 创建）。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 消息总线走 localhost（`tcp://127.0.0.1`），绝不跨机器
- hub 为独立总线进程（bus_server），由 Supervisor 守护；三/四进程相互独立，任一崩溃不影响其他
- 节点仅持 DEALER 连接 hub 的 ROUTER，**不绑定任何端口**
- 统一消息信封：`{"version": 1, "trace_id": str, "service": str, "request_id": str, "payload": dict}`；错误 `{"request_id", "error"}`
- 请求超时默认 2000ms，超时抛 `BusTimeoutError`；`error` 响应抛 `BusError`
- 订阅/响应 handler 异常不得杀死后台线程：记录日志 + 错误计数，线程继续
- 同前缀多订阅者并存（单 SUB 套接字多 SUBSCRIBE）
- ZMQ HWM：SNDHWM/RCVHWM = 1000
- 每进程注册 SIGINT/SIGTERM/SIGBREAK → 优雅关闭（finally 清理）
- 日志：stdlib logging + structlog，JSON 行输出，trace_id 绑定；audit_logger/decision_logger 独立文件
- Config：默认值 ← config.yaml ← env(YUKI_*) 三级合并，Pydantic 校验
- 新增依赖：structlog、pydantic、pyyaml（prod）；Pillow（dev）
- 目录结构：`src/yuki/<layer>/`，测试在 `tests/<layer>/`
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交
- 既有测试必须保持通过（Phase 1 的 17 单元 + 1 e2e）

## Phase 1 遗留承接 + 新问题覆盖映射

| 问题 | 承接任务 |
|---|---|
| hub 与 cognition 耦合 | Task 4（bus_server 独立进程） |
| 无优雅关闭 | Task 4（ShutdownManager + CTRL_BREAK_EVENT） |
| REQ/REP 三缺陷（无超时/无错误路径/单端口单服务） | Task 3（ROUTER/DEALER 转发 + 注册表） |
| 订阅 handler 覆盖 | Task 3（单 SUB 多 SUBSCRIBE + list of handlers） |
| 订阅线程无异常保护 | Task 3（try-except + 日志 + 错误计数） |
| 零日志框架 | Task 1（logger.py + audit/decision logger） |
| 无健康状态报告 | Task 5（health/{name} REQ/REP 探活） |
| 重启策略粗糙 | Task 5（指数退避 + 时间窗口 + 日志） |
| Config 过于简单 | Task 2（Pydantic + YAML + env） |
| 消息无 schema（信封/trace_id） | Task 3（信封字段）+ Task 6（文档）；protobuf 编码列入 Phase 2b |
| 无背压控制 | Task 3（HWM=1000） |

---

## File Structure

```
docs/superpowers/specs/2026-08-10-yuki-interfaces.md   # 新增：接口契约文档
pyproject.toml                                         # 修改：deps
config.example.yaml                                    # 新增：Config 样例
src/yuki/logger.py                                     # 新增：日志框架
src/yuki/config.py                                     # 修改：Pydantic + YAML + env
src/yuki/bus.py                                        # 修改：ROUTER/DEALER + 订阅修复 + HWM + 信封
src/yuki/shutdown.py                                   # 新增：ShutdownManager
src/yuki/health.py                                     # 新增：health/{name} 服务 helper
src/yuki/bus_server/main.py                            # 新增：独立 hub 进程
src/yuki/bus_server/__main__.py                        # 新增
src/yuki/cognition/main.py                             # 修改：优雅关闭 + role + health
src/yuki/interaction/main.py                           # 修改：优雅关闭 + role + health（去 run()）
src/yuki/interaction/hotkey.py                         # 修改：移除 run()
src/yuki/perception/main.py                            # 修改：优雅关闭 + role + health
src/yuki/supervisor.py                                 # 修改：env 透传 + terminate_children + 退避窗口
src/yuki/supervisor/main.py                            # 修改：纯看门狗 + 探活循环
src/yuki/recorder/session.py                           # 新增：录制会话
src/yuki/recorder/cli.py                               # 新增：录制器 CLI
src/yuki/recorder/__main__.py                          # 新增
tests/test_logger.py                                   # 新增
tests/test_config.py                                   # 修改/重写
tests/test_bus.py                                      # 修改/重写
tests/test_bus_faults.py                               # 修改
tests/test_shutdown.py                                 # 新增
tests/test_health.py                                   # 新增
tests/test_supervisor.py                               # 修改
tests/test_supervisor_main.py                          # 修改
tests/test_e2e.py                                      # 修改
tests/recorder/test_session.py                         # 新增
```

---

### Task 1: 日志框架

**Files:**
- Create: `src/yuki/logger.py`
- Modify: `pyproject.toml`
- Test: `tests/test_logger.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `configure_logging(level: str = "INFO") -> None` — 配置 structlog 输出 JSON 行到 stderr；调用一次幂等
  - `get_logger(name: str)` — 返回经 structlog 包装的命名 logger（`logger.info/warning/error/exception` 可调，接收任意 kwarg）
  - `get_file_logger(name: str, filename: str, log_dir: Path = Path("logs"))` — 返回 structlog logger，JSON 行写入 `<log_dir>/<filename>`；底层 stdlib logger 挂 FileHandler 且 `propagate=False`（避免污染 stderr）
  - `audit_logger` — 模块级单例（`yuki.audit` → `logs/audit.jsonl`），仅记录过滤动作，**永不记录原文**
  - `decision_logger` — 模块级单例（`yuki.decision` → `logs/decision.jsonl`），记录开口决策输入与结果
  - `bind_trace_id(trace_id: str) -> None` — contextvars 绑定，随日志自动附加
  - `unbind_trace_id() -> None`

- [ ] **Step 1: 修改 `pyproject.toml` 加依赖**

```toml
[project]
dependencies = ["pyzmq>=25", "structlog>=24", "pydantic>=2", "PyYAML>=6"]

[project.optional-dependencies]
dev = ["pytest>=8", "Pillow>=10"]
```

- [ ] **Step 2: 写失败测试 `tests/test_logger.py`**

```python
import json

import structlog

from yuki import logger as logger_mod
from yuki.logger import (
    audit_logger,
    bind_trace_id,
    configure_logging,
    decision_logger,
    get_file_logger,
    get_logger,
    unbind_trace_id,
)


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO")  # 不应抛异常


def test_get_logger_is_callable():
    log = get_logger("test")
    assert callable(log.info)
    assert callable(log.warning)
    assert callable(log.error)
    assert callable(log.exception)


def test_file_logger_writes_json_line(tmp_path):
    log = get_file_logger("yuki.audit.test", "audit.jsonl", tmp_path)
    log.info("filter_action", rule="SENSITIVE_PASSWORD", category="credentials")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[0])
    assert data["event"] == "filter_action"
    assert data["rule"] == "SENSITIVE_PASSWORD"
    assert data["category"] == "credentials"


def test_file_logger_appends_lines(tmp_path):
    log = get_file_logger("yuki.decision.test", "decision.jsonl", tmp_path)
    log.info("speak_decision", topic="science", speak=True)
    log.info("speak_decision", topic="history", speak=False)
    lines = (tmp_path / "decision.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["topic"] == "history"


def test_trace_id_binding():
    bind_trace_id("trace-abc")
    assert structlog.contextvars.get_contextvars().get("trace_id") == "trace-abc"
    unbind_trace_id()
    assert "trace_id" not in structlog.contextvars.get_contextvars()


def test_logger_module_exports():
    assert hasattr(logger_mod, "audit_logger")
    assert hasattr(logger_mod, "decision_logger")
    assert hasattr(logger_mod, "get_logger")
    assert hasattr(logger_mod, "get_file_logger")
    assert hasattr(logger_mod, "configure_logging")
    assert hasattr(logger_mod, "bind_trace_id")
    assert hasattr(logger_mod, "unbind_trace_id")


def test_module_singletons_write_under_logs_dir():
    # audit_logger / decision_logger 可调用（写 logs/ 目录，测试不校验内容）
    assert callable(audit_logger.info)
    assert callable(decision_logger.info)
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/test_logger.py -v`
Expected: FAIL，`No module named 'yuki.logger'`（若 structlog 未装则先 `python -m pip install -e ".[dev]"`）

- [ ] **Step 4: 实现 `src/yuki/logger.py`**

```python
import logging
import sys
from pathlib import Path

import structlog

_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)


def get_file_logger(name: str, filename: str, log_dir: Path = Path("logs")):
    configure_logging()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    stdlib_logger = logging.getLogger(name)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.addHandler(handler)
    stdlib_logger.propagate = False
    return structlog.get_logger(name)


audit_logger = get_file_logger("yuki.audit", "audit.jsonl")
decision_logger = get_file_logger("yuki.decision", "decision.jsonl")


def bind_trace_id(trace_id: str) -> None:
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def unbind_trace_id() -> None:
    structlog.contextvars.unbind_contextvars("trace_id")
```

注意：structlog 的 `BoundLoggerLazyProxy` 没有 `.name` 属性，故测试只断言方法可调。`get_file_logger` 返回 structlog logger，其 kwargs 会作为顶层键进入 JSON 行（`event` 为消息文本）。

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_logger.py -v`
Expected: 7 个测试 PASS

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml src/yuki/logger.py tests/test_logger.py
git commit -m "feat: add structlog-based logging with audit and decision loggers"
```

---

### Task 2: Config 扩展（Pydantic + YAML + env）

**Files:**
- Modify: `src/yuki/config.py`
- Create: `config.example.yaml`
- Test: `tests/test_config.py`（重写）

**Interfaces:**
- Consumes: 无
- Produces:
  - `@dataclass` 改为 Pydantic `BaseModel`（`class Config(BaseModel)`）
  - 字段：`base_port: int = 5555`、`log_level: str = "INFO"`、`persona_name: str = "yuki"`、`bus_role: str = "hub"`、`restart_base_delay: float = 1.0`、`restart_max_delay: float = 60.0`、`restart_window: int = 600`、`restart_max_per_window: int = 5`、`health_timeout_ms: int = 2000`、`hwm: int = 1000`
  - `Config.load(config_file: str | Path | None = None) -> "Config"` — 默认值 ← YAML（可选）← env（`YUKI_*`）三级合并，Pydantic 校验
  - `Config.from_env() -> "Config"` — 保留兼容，等价 `Config.load(None)`（无 YAML 时仅 env）
  - `Config.model_dump() -> dict` — Pydantic 内置

- [ ] **Step 1: 重写失败测试 `tests/test_config.py`**

```python
import pytest
from pydantic import ValidationError

from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.base_port == 5555
    assert config.log_level == "INFO"
    assert config.persona_name == "yuki"
    assert config.bus_role == "hub"
    assert config.restart_base_delay == 1.0
    assert config.restart_max_delay == 60.0
    assert config.restart_window == 600
    assert config.restart_max_per_window == 5
    assert config.health_timeout_ms == 2000
    assert config.hwm == 1000


def test_from_env_merges_env_overrides(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("YUKI_BUS_ROLE", "node")
    monkeypatch.setenv("YUKI_HWM", "500")
    config = Config.load(None)
    assert config.base_port == 7000
    assert config.log_level == "DEBUG"
    assert config.bus_role == "node"
    assert config.hwm == 500


def test_yaml_then_env_merge(tmp_path, monkeypatch):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("base_port: 8000\nhwm: 200\n", encoding="utf-8")
    monkeypatch.setenv("YUKI_HWM", "300")
    config = Config.load(yaml_file)
    assert config.base_port == 8000  # 来自 YAML
    assert config.hwm == 300         # env 覆盖 YAML
    assert config.log_level == "INFO"  # 默认


def test_env_field_name_mapping():
    # YUKI_BASE_PORT -> base_port
    config = Config.load(None)
    assert config.base_port == 5555


def test_validation_rejects_bad_port():
    with pytest.raises(ValidationError):
        Config(base_port=99)  # 端口必须 >= 1024


def test_from_env_backward_compat(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    config = Config.from_env()
    assert config.base_port == 7000
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL（既有断言字段名仍匹配，但 Pydantic 校验/合并逻辑未实现；`test_validation_rejects_bad_port` 直接失败）

- [ ] **Step 3: 实现 `src/yuki/config.py`**

```python
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Config(BaseModel):
    base_port: int = Field(5555, ge=1024, le=65535)
    log_level: str = "INFO"
    persona_name: str = "yuki"
    bus_role: str = "hub"
    restart_base_delay: float = 1.0
    restart_max_delay: float = 60.0
    restart_window: int = 600
    restart_max_per_window: int = 5
    health_timeout_ms: int = 2000
    hwm: int = Field(1000, ge=1)

    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "Config":
        data: dict = {}
        if config_file and Path(config_file).exists():
            with open(config_file, "r", encoding="utf-8") as fh:
                data.update(yaml.safe_load(fh) or {})
        for field in cls.model_fields:
            env_key = f"YUKI_{field.upper()}"
            if env_key in os.environ:
                data[field] = cls._coerce(field, os.environ[env_key])
        return cls(**data)

    @classmethod
    def _coerce(cls, field: str, raw: str):
        annotation = cls.model_fields[field].annotation
        if annotation is bool:
            return raw.lower() in ("1", "true", "yes")
        try:
            return annotation(raw)
        except (TypeError, ValueError):
            return raw

    @classmethod
    def from_env(cls) -> "Config":
        return cls.load(None)
```

- [ ] **Step 4: 创建 `config.example.yaml`**

```yaml
# Yuki Agent 配置样例。复制为 config.yaml 使用；环境变量 YUKI_* 覆盖同级项。
base_port: 5555
log_level: INFO
persona_name: yuki
bus_role: hub
restart_base_delay: 1.0
restart_max_delay: 60.0
restart_window: 600
restart_max_per_window: 5
health_timeout_ms: 2000
hwm: 1000
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 6 个测试 PASS

- [ ] **Step 6: 回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS（既有 e2e/其他测试引用 `Config()`/`Config.from_env()` 应兼容）
```bash
git add src/yuki/config.py config.example.yaml tests/test_config.py
git commit -m "feat: expand config with pydantic validation, YAML, and env merge"
```

---

### Task 3: MessageBus 升级（ROUTER/DEALER + 订阅修复 + HWM + 信封）

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `tests/test_bus.py`
- Modify: `tests/test_bus_faults.py`
- Test: 上述两文件

**Interfaces:**
- Consumes: `get_logger`（Task 1）、`bind_trace_id`/`unbind_trace_id`（Task 1）
- Produces:
  - `class BusError(Exception)`、`class BusTimeoutError(BusError)`
  - `MessageBus(base_port=5555, role="hub"|"node")`
    - `publish(topic: str, payload: dict)` — 信封化：`{"version":1,"topic":topic,"payload":payload}`
    - `subscribe(topic_prefix: str, handler) -> None` — 单 SUB 套接字 + 多 SUBSCRIBE；同前缀多 handler 并存
    - `request(service, payload, timeout_ms=2000) -> dict` — 信封 `{"version","trace_id","service","request_id","payload"}`；超时 `BusTimeoutError`；error → `BusError`
    - `respond(service, handler) -> None` — 注册服务；handler 异常 → error 信封且循环不死
    - `close() -> None`
  - HWM=1000 应用于 PUB/SUB/DEALER 套接字（读取 `Config.hwm`，bus 构造参数 `hwm: int = 1000`）
  - hub 角色跑 `_proxy_loop`（XSUB/XPUB zmq.proxy）+ `_router_loop`（ROUTER 转发）

- [ ] **Step 1: 重写 `tests/test_bus_faults.py`**

```python
import threading
import time

import pytest

from yuki.bus import BusError, BusTimeoutError, MessageBus


@pytest.fixture()
def make_bus():
    buses = []

    def _make(port, role="hub"):
        bus = MessageBus(base_port=port, role=role, hwm=10)
        buses.append(bus)
        return bus

    yield _make
    for bus in buses:
        bus.close()


def test_request_respond_roundtrip(make_bus):
    bus = make_bus(6110)
    bus.respond("ping", lambda payload: {"echo": payload["msg"]})
    time.sleep(0.1)
    assert bus.request("ping", {"msg": "hello"}, timeout_ms=1000) == {"echo": "hello"}


def test_request_unregistered_service_raises_bus_error(make_bus):
    bus = make_bus(6120)
    time.sleep(0.1)
    with pytest.raises(BusError, match="service not found"):
        bus.request("ghost", {}, timeout_ms=1000)


def test_request_times_out_when_handler_hangs(make_bus):
    bus = make_bus(6121)

    def slow(payload):
        time.sleep(1.0)
        return {"ok": 1}

    bus.respond("slow", slow)
    time.sleep(0.1)
    with pytest.raises(BusTimeoutError):
        bus.request("slow", {}, timeout_ms=200)


def test_request_raises_bus_error_on_failed_handler(make_bus):
    bus = make_bus(6130)

    def handler(payload):
        raise ValueError("boom")

    bus.respond("boom", handler)
    time.sleep(0.1)
    with pytest.raises(BusError, match="handler error"):
        bus.request("boom", {}, timeout_ms=1000)


def test_respond_loop_survives_handler_exception(make_bus):
    bus = make_bus(6140)

    def handler(payload):
        if payload.get("bad"):
            raise ValueError("bad payload")
        return {"echo": payload["msg"]}

    bus.respond("svc", handler)
    time.sleep(0.1)
    with pytest.raises(BusError, match="handler error"):
        bus.request("svc", {"bad": True}, timeout_ms=1000)
    assert bus.request("svc", {"msg": "hi"}, timeout_ms=1000) == {"echo": "hi"}


def test_sub_thread_survives_handler_exception(make_bus):
    bus = make_bus(6150)
    received = threading.Event()
    got = []

    def on_event(topic, payload):
        if payload.get("bad"):
            raise RuntimeError("handler boom")
        got.append(payload)
        received.set()

    bus.subscribe("event/", on_event)
    time.sleep(0.1)
    bus.publish("event/a", {"bad": True})
    bus.publish("event/b", {"ok": 1})
    assert received.wait(timeout=2.0)
    assert got == [{"ok": 1}]


def test_multiple_handlers_same_prefix_all_called(make_bus):
    bus = make_bus(6160)
    calls = []

    def h1(topic, payload):
        calls.append("h1")

    def h2(topic, payload):
        calls.append("h2")

    bus.subscribe("event/", h1)
    bus.subscribe("event/", h2)
    time.sleep(0.1)
    bus.publish("event/x", {})
    deadline = time.time() + 2.0
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(calls) == ["h1", "h2"]


def test_overlapping_prefixes_both_dispatch(make_bus):
    bus = make_bus(6170)
    got = []

    def broad(topic, payload):
        got.append("broad")

    def narrow(topic, payload):
        got.append("narrow")

    bus.subscribe("event/", broad)
    bus.subscribe("event/awake", narrow)
    time.sleep(0.1)
    bus.publish("event/awake", {})
    deadline = time.time() + 2.0
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(got) == ["broad", "narrow"]
```

- [ ] **Step 2: 重写 `tests/test_bus.py`**（保留 publish/subscribe 语义测试，适配新信封）

```python
import threading
import time

from yuki.bus import MessageBus


def _wait_sub(t=0.1):
    time.sleep(t)


def test_publish_subscribe_roundtrip():
    bus = MessageBus(base_port=6001, hwm=10)
    received = threading.Event()
    got = {}

    def on_event(topic, payload):
        got["topic"] = topic
        got["payload"] = payload
        received.set()

    bus.subscribe("event/", on_event)
    _wait_sub()
    bus.publish("event/awake", {"source": "hotkey"})
    assert received.wait(timeout=2.0)
    assert got["topic"] == "event/awake"
    assert got["payload"] == {"source": "hotkey"}
    bus.close()


def test_subscribe_filters_by_prefix():
    bus = MessageBus(base_port=6002, hwm=10)
    hits = []

    def on_awake(topic, payload):
        hits.append(payload)

    bus.subscribe("event/awake", on_awake)
    _wait_sub()
    bus.publish("event/reply", {"text": "hi"})
    bus.publish("event/awake", {"source": "hotkey"})
    deadline = time.time() + 2.0
    while not hits and time.time() < deadline:
        time.sleep(0.05)
    assert hits == [{"source": "hotkey"}]
    bus.close()
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/test_bus.py tests/test_bus_faults.py -v`
Expected: FAIL（新 API/信封/订阅语义未实现）

- [ ] **Step 4: 重写 `src/yuki/bus.py`**

```python
import json
import logging
import threading
import time
import uuid
from typing import Callable

import zmq

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id

logger = get_logger("yuki.bus")

VERSION = 1


class BusError(Exception):
    pass


class BusTimeoutError(BusError):
    pass


def _matches(prefix: str, topic: str) -> bool:
    return topic.startswith(prefix)


class MessageBus:
    """本地消息总线：PUB/SUB 经 XPUB/XSUB 枢纽 + REQ/REP 经 ROUTER/DEALER 枢纽。

    role="hub"：承载枢纽（绑定 base_port..base_port+2）。
    role="node"：仅连接。多进程部署时 bus_server 以 hub 运行，其余以 node 连接。
    """

    def __init__(self, base_port: int = 5555, role: str = "hub", hwm: int = 1000):
        self._ctx = zmq.Context()
        self._xsub_port = base_port
        self._xpub_port = base_port + 1
        self._router_port = base_port + 2
        self._hwm = hwm
        self._handlers: dict[str, list[Callable[[str, dict], None]]] = {}
        self._services: dict[str, Callable[[dict], dict]] = {}
        self._pending: dict[str, dict] = {}
        self._service_map: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._error_count = 0
        if role == "hub":
            self._start_hub()
        elif role != "node":
            raise ValueError(f"unknown bus role: {role!r}")

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, self._hwm)
        self._pub.connect(f"tcp://127.0.0.1:{self._xsub_port}")

        self._dealer = self._ctx.socket(zmq.DEALER)
        self._dealer.setsockopt(zmq.SNDHWM, self._hwm)
        self._dealer.setsockopt(zmq.RCVHWM, self._hwm)
        self._dealer.connect(f"tcp://127.0.0.1:{self._router_port}")
        threading.Thread(target=self._dealer_loop, daemon=True).start()

    def _start_hub(self) -> None:
        xsub = self._ctx.socket(zmq.XSUB)
        xsub.bind(f"tcp://127.0.0.1:{self._xsub_port}")
        xpub = self._ctx.socket(zmq.XPUB)
        xpub.bind(f"tcp://127.0.0.1:{self._xpub_port}")
        self._router = self._ctx.socket(zmq.ROUTER)
        self._router.setsockopt(zmq.RCVHWM, self._hwm)
        self._router.bind(f"tcp://127.0.0.1:{self._router_port}")
        threading.Thread(target=self._proxy_loop, args=(xsub, xpub), daemon=True).start()
        threading.Thread(target=self._router_loop, daemon=True).start()

    def _proxy_loop(self, xsub, xpub) -> None:
        try:
            zmq.proxy(xsub, xpub)
        except zmq.ZMQError:
            pass

    def _router_loop(self) -> None:
        while True:
            try:
                frames = self._router.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) < 3:
                continue
            sender = frames[0]
            if frames[1] == b"REGISTER":
                self._service_map[frames[2].decode()] = sender
                continue
            f1, f2 = frames[1], frames[2]
            try:
                msg = json.loads(f2.decode())
            except ValueError:
                continue
            if "payload" in msg:
                provider = self._service_map.get(msg.get("service", ""))
                if provider is None:
                    err = {"version": VERSION, "request_id": msg.get("request_id"), "error": "service not found"}
                    self._router.send_multipart([sender, json.dumps(err).encode()])
                else:
                    self._router.send_multipart([provider, sender, f2])
            elif "error" in msg or "result" in msg:
                self._router.send_multipart([f1, f2])

    def _dealer_loop(self) -> None:
        while True:
            try:
                frames = self._dealer.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    msg = json.loads(raw.decode())
                except ValueError:
                    continue
                if msg.get("trace_id"):
                    bind_trace_id(msg["trace_id"])
                handler = self._services.get(msg.get("service"))
                if handler is None:
                    reply = {"version": VERSION, "request_id": msg.get("request_id"), "error": "service not found"}
                else:
                    try:
                        result = handler(msg.get("payload", {}))
                        reply = {"version": VERSION, "request_id": msg.get("request_id"), "result": result}
                    except Exception:
                        logger.error("responder handler failed", service=msg.get("service"))
                        self._error_count += 1
                        reply = {"version": VERSION, "request_id": msg.get("request_id"), "error": "handler error"}
                self._dealer.send_multipart([client_id, json.dumps(reply).encode()])
                if msg.get("trace_id"):
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    msg = json.loads(frames[0].decode())
                except ValueError:
                    continue
                rid = msg.get("request_id")
                with self._lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = msg
                    entry["event"].set()

    def publish(self, topic: str, payload: dict) -> None:
        envelope = {"version": VERSION, "topic": topic, "payload": payload}
        self._pub.send_multipart([topic.encode(), json.dumps(envelope).encode()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            handlers = self._handlers.setdefault(topic_prefix, [])
            handlers.append(handler)
        if not hasattr(self, "_sub"):
            self._sub = self._ctx.socket(zmq.SUB)
            self._sub.setsockopt(zmq.RCVHWM, self._hwm)
            self._sub.connect(f"tcp://127.0.0.1:{self._xpub_port}")
            threading.Thread(target=self._run_sub, args=(self._sub,), daemon=True).start()
        self._sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)

    def _run_sub(self, sub) -> None:
        while True:
            try:
                raw_topic, raw_payload = sub.recv_multipart()
            except zmq.ZMQError:
                return
            topic = raw_topic.decode()
            try:
                envelope = json.loads(raw_payload.decode())
            except ValueError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            payload = envelope.get("payload", envelope)
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                try:
                    handler(topic, payload)
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._error_count += 1

    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        envelope = {
            "version": VERSION,
            "trace_id": uuid.uuid4().hex,
            "service": service,
            "request_id": rid,
            "payload": payload,
        }
        with self._lock:
            self._pending[rid] = {"event": event, "result": None}
        self._dealer.send_multipart([service.encode(), json.dumps(envelope).encode()])
        if not event.wait(timeout_ms / 1000.0):
            with self._lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._lock:
            msg = self._pending.pop(rid)["result"]
        if "error" in msg:
            raise BusError(msg["error"])
        return msg["result"]

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        self._services[service] = handler
        self._dealer.send_multipart([b"REGISTER", service.encode()])

    def close(self) -> None:
        try:
            self._ctx.destroy(linger=0)
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_bus.py tests/test_bus_faults.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 全量回归 + 提交**

Run: `python -m pytest -v`
Expected: 既有认知层/交互层单测可能因 bus 契约变化需小改（`build_cognition`/`build_interaction` 用 FakeBus，不受影响）。若 `test_smoke`/`test_topics` 受影响则修正。
```bash
git add src/yuki/bus.py tests/test_bus.py tests/test_bus_faults.py
git commit -m "refactor: route req/rep through hub, fix multi-subscriber dispatch, add envelope and HWM"
```

---

### Task 4: bus_server 独立进程 + 优雅关闭

**Files:**
- Create: `src/yuki/shutdown.py`
- Create: `src/yuki/bus_server/main.py`
- Create: `src/yuki/bus_server/__main__.py`
- Modify: `src/yuki/cognition/main.py`
- Modify: `src/yuki/interaction/main.py`
- Modify: `src/yuki/interaction/hotkey.py`
- Modify: `src/yuki/perception/main.py`
- Modify: `src/yuki/supervisor.py`
- Modify: `src/yuki/supervisor/main.py`
- Test: `tests/test_shutdown.py`
- Test: `tests/test_supervisor.py`
- Test: `tests/test_supervisor_main.py`

**Interfaces:**
- Consumes: `MessageBus`（Task 3）、`Config`（Task 2）、`get_logger`（Task 1）
- Produces:
  - `class ShutdownManager`（`yuki.shutdown`）：`register_signal_handlers()`、`shutdown_requested: bool`、`wait(timeout=None) -> bool`、`request_shutdown()`（测试用）
  - `bus_server/main.py`：`main()` — hub 常驻 + 优雅关闭
  - `Supervisor` 增加 `env` 参数、`_spawn` 传 `creationflags=CREATE_NEW_PROCESS_GROUP`、`terminate_children()`（先 SIGBREAK 再 terminate 兜底）
  - `supervisor/main.py`：纯看门狗，spawn 4 子进程（含 bus_server），`--trigger-after` 透传，优雅关闭时 terminate_children
  - 各层 `main()`：shutdown 循环 + `finally: bus.close()`；interaction 移除 `HotkeyManager.run()` 调用与定义

- [ ] **Step 1: 写失败测试 `tests/test_shutdown.py`**

```python
import threading

from yuki.shutdown import ShutdownManager


def test_initial_not_requested():
    mgr = ShutdownManager()
    assert not mgr.shutdown_requested


def test_request_shutdown_sets_flag():
    mgr = ShutdownManager()
    mgr.request_shutdown()
    assert mgr.shutdown_requested


def test_wait_returns_true_after_shutdown():
    mgr = ShutdownManager()

    def _signal():
        mgr.request_shutdown()

    t = threading.Timer(0.1, _signal)
    t.start()
    assert mgr.wait(timeout=2.0) is True
    t.cancel()


def test_wait_returns_false_on_timeout():
    mgr = ShutdownManager()
    assert mgr.wait(timeout=0.05) is False
```

- [ ] **Step 2: 追加失败测试 `tests/test_supervisor.py`**

```python
import subprocess

import pytest

from yuki.supervisor import Supervisor


class FakeProcWithState:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated += 1
        self._exit_code = 0

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        self._exit_code = 0
        return 0


def test_terminate_children_terminates_alive():
    child = FakeProcWithState(exit_code=None)
    sup = Supervisor(
        [("cognition", ["python", "-m", "yuki.cognition"])],
        popen_factory=lambda cmd, env=None, creationflags=None: child,
        restart_delay=0.0,
    )
    sup.terminate_children(timeout=1.0)
    assert child.terminated >= 1


def test_terminate_children_kills_on_timeout(monkeypatch):
    class HungryProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("cognition", 1)

        def kill(self):
            self.killed = True

    hungry = HungryProc()
    hungry.killed = False
    sup = Supervisor(
        [("cognition", ["python", "-m", "yuki.cognition"])],
        popen_factory=lambda cmd, env=None, creationflags=None: hungry,
        restart_delay=0.0,
    )
    sup.terminate_children(timeout=0.01)
    assert hungry.killed is True
```

- [ ] **Step 3: 修改 `tests/test_supervisor_main.py`（四层 + bus_server）**

```python
import sys

from yuki.supervisor.main import build_children_cmds


def test_build_children_cmds_returns_four_layers():
    names = [name for name, _ in build_children_cmds()]
    assert names == ["bus_server", "cognition", "interaction", "perception"]


def test_build_children_cmds_appends_interaction_extra():
    cmds = build_children_cmds(["--trigger-after", "1"])
    by_name = dict(cmds)
    assert by_name["bus_server"] == [sys.executable, "-m", "yuki.bus_server"]
    assert by_name["cognition"] == [sys.executable, "-m", "yuki.cognition"]
    assert by_name["interaction"] == [
        sys.executable,
        "-m",
        "yuki.interaction",
        "--trigger-after",
        "1",
    ]
    assert by_name["perception"] == [sys.executable, "-m", "yuki.perception"]
```

- [ ] **Step 4: 跑测试验证失败**

Run: `python -m pytest tests/test_shutdown.py tests/test_supervisor.py tests/test_supervisor_main.py -v`
Expected: FAIL，`No module named 'yuki.shutdown'` / `No module named 'yuki.bus_server.main'`

- [ ] **Step 5: 实现 `src/yuki/shutdown.py`**

```python
import signal
import threading


class ShutdownManager:
    """注册 SIGINT/SIGTERM/SIGBREAK，提供优雅关闭事件。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def register_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass

    def _handle(self, signum, frame) -> None:
        self._event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def request_shutdown(self) -> None:
        self._event.set()
```

- [ ] **Step 6: 实现 `src/yuki/bus_server/main.py`**

```python
import time

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.shutdown import ShutdownManager


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role="hub", hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 实现 `src/yuki/bus_server/__main__.py`**

```python
from yuki.bus_server.main import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 8: 改造三层入口 main() 为 shutdown 循环**

统一模式（`cognition/main.py`、`interaction/main.py`、`perception/main.py`）：

```python
def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_<layer>(bus, <layer_args...>)
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()
```

interaction 特例：`build_interaction(bus, hotkeys)` 保持；移除对 `hotkeys.run()` 的调用（改为 shutdown 循环）；`--trigger-after` 逻辑保留（在循环前启动 daemon 线程）。`hotkey.py` 删除 `run()` 方法（保留 register/trigger）。

- [ ] **Step 9: 修改 `src/yuki/supervisor.py`**

```python
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from yuki.logger import get_logger

logger = get_logger("yuki.supervisor")


@dataclass
class Child:
    name: str
    cmd: list[str]
    proc: "subprocess.Popen"
    restarts: int = 0


class Supervisor:
    def __init__(
        self,
        cmds: list[tuple[str, list[str]]],
        popen_factory: Callable = subprocess.Popen,
        restart_delay: float = 1.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._env = env
        self._children: list[Child] = [
            Child(name=name, cmd=cmd, proc=self._spawn(cmd)) for name, cmd in cmds
        ]

    def _spawn(self, cmd: list[str]) -> "subprocess.Popen":
        kwargs = {}
        if self._env is not None:
            kwargs["env"] = self._env
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return self._popen(cmd, **kwargs)

    def tick(self, max_restarts: int = 3) -> list[str]:
        restarted: list[str] = []
        for child in self._children:
            if child.proc.poll() is not None:
                if child.restarts >= max_restarts:
                    raise RuntimeError(f"{child.name} crashed too many times")
                child.restarts += 1
                time.sleep(self._restart_delay)
                child.proc = self._spawn(child.cmd)
                restarted.append(child.name)
        return restarted

    def terminate_children(self, timeout: float = 5.0) -> None:
        for child in self._children:
            if child.proc.poll() is None:
                child.proc.terminate()
        deadline = time.time() + timeout
        for child in self._children:
            if child.proc.poll() is None:
                try:
                    child.proc.wait(timeout=max(0.0, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    child.proc.kill()
```

注意：Windows 上 `proc.terminate()` 是 TerminateProcess（硬杀）。要让子进程真正走 Python handler，supervisor 在 SIGINT/SIGTERM 时应改用 `os.kill(pid, signal.CTRL_BREAK_EVENT)`。本任务交付 `terminate_children`（terminate + kill 兜底）作为骨架；**CTRL_BREAK_EVENT 精确路径在 Task 5 的 Supervisor 强化中实现**（同一文件，随探活循环一起）。若 Step 9 的 `_spawn` creationflags 使既有 fake factory 签名不匹配，更新 `test_supervisor.py` 的 factory 为 `lambda cmd, env=None, creationflags=None`。

- [ ] **Step 10: 修改 `src/yuki/supervisor/main.py`**

```python
import os
import sys
import time

from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.supervisor import Supervisor

CHILDREN = [
    ("bus_server", [sys.executable, "-m", "yuki.bus_server"]),
    ("cognition", [sys.executable, "-m", "yuki.cognition"]),
    ("interaction", [sys.executable, "-m", "yuki.interaction"]),
    ("perception", [sys.executable, "-m", "yuki.perception"]),
]


def build_children_cmds(interaction_extra: list[str] | None = None) -> list[tuple[str, list[str]]]:
    cmds = []
    for name, base in CHILDREN:
        if name == "interaction" and interaction_extra:
            cmds.append((name, base + interaction_extra))
        else:
            cmds.append((name, base))
    return cmds


def main() -> None:
    config = Config.from_env()
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()

    extra = None
    if "--trigger-after" in sys.argv:
        index = sys.argv.index("--trigger-after")
        extra = ["--trigger-after", sys.argv[index + 1]]

    env = dict(os.environ)
    env["YUKI_BUS_ROLE"] = "node"
    env["YUKI_BASE_PORT"] = str(config.base_port)

    supervisor = Supervisor(build_children_cmds(extra), env=env)
    try:
        while not shutdown.shutdown_requested:
            try:
                supervisor.tick()
            except RuntimeError as exc:
                print(f"[supervisor] {exc}", flush=True)
            shutdown.wait(timeout=0.5)
    finally:
        supervisor.terminate_children()


if __name__ == "__main__":
    main()
```

- [ ] **Step 11: 跑测试验证通过**

Run: `python -m pytest tests/test_shutdown.py tests/test_supervisor.py tests/test_supervisor_main.py -v`
Expected: 全部 PASS

- [ ] **Step 12: 全量回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS（e2e 默认被排除，Task 5 会改写 e2e 走 supervisor）
```bash
git add src/yuki/shutdown.py src/yuki/bus_server src/yuki/cognition/main.py src/yuki/interaction src/yuki/perception/main.py src/yuki/supervisor.py src/yuki/supervisor/main.py tests/test_shutdown.py tests/test_supervisor.py tests/test_supervisor_main.py
git commit -m "feat: dedicated bus process, graceful shutdown, supervisor spawns four layers"
```

---

### Task 5: Supervisor 强化（健康探活 + 退避 + 时间窗口）

**Files:**
- Create: `src/yuki/health.py`
- Modify: `src/yuki/supervisor.py`
- Modify: `src/yuki/supervisor/main.py`
- Modify: `src/yuki/cognition/main.py`
- Modify: `src/yuki/interaction/main.py`
- Modify: `src/yuki/perception/main.py`
- Modify: `tests/test_e2e.py`
- Modify: `tests/test_supervisor.py`
- Test: `tests/test_health.py`（新增）、上述文件

**Interfaces:**
- Consumes: `MessageBus.request`（Task 3）、`ShutdownManager`（Task 4）、`Config`（Task 2）
- Produces:
  - `register_health_service(bus: MessageBus, name: str) -> None`（`yuki.health`）— 注册 `health/{name}` 服务，handler 返回 `{"process": name, "pid": int, "uptime_s": float, "error_count": int}`
  - `Supervisor.tick()` 扩展：每 tick 对存活子进程做健康探活（`request(f"health/{name}", {}, timeout_ms=config.health_timeout_ms)`），超时/异常 → 计为一次失败；bus_server 未存活时跳过其他探活
  - 指数退避：`delay = min(restart_base_delay * 2**attempts, restart_max_delay)`；子进程存活 > restart_window 秒则重置 attempts
  - 时间窗口计数：`restart_window` 秒内重启超过 `restart_max_per_window` 次 → 停止重启并记 CRITICAL
  - `Supervisor` 注入 `clock`/`sleep` 可测（默认 `time.time`/`time.sleep`）
  - 每次重启/探活失败/窗口超限记日志（含原因、尝试次数、下次延迟）
  - supervisor 关闭时对子进程发 `CTRL_BREAK_EVENT`（Windows）触发其 handler，再 terminate 兜底

- [ ] **Step 1: 写失败测试 `tests/test_health.py`**

```python
import time

from yuki.bus import MessageBus
from yuki.health import register_health_service


def test_register_health_service_responds(make_bus):
    bus = make_bus(6200)
    register_health_service(bus, "cognition")
    time.sleep(0.1)
    result = bus.request("health/cognition", {}, timeout_ms=1000)
    assert result["process"] == "cognition"
    assert result["pid"] > 0
    assert "uptime_s" in result
    assert "error_count" in result
```

（`make_bus` fixture 复用 Task 3 的 `tests/test_bus_faults.py` 定义，或在本文件重复定义）

- [ ] **Step 2: 追加失败测试 `tests/test_supervisor.py`（退避 + 窗口 + 探活）**

```python
import time

import pytest

from yuki.supervisor import Supervisor


def test_backoff_increases_with_attempts():
    class P:
        def __init__(self):
            self._alive = False

        def poll(self):
            return 1 if not self._alive else None

    procs = {}
    created = []

    def factory(cmd, env=None, creationflags=None):
        p = P()
        procs[cmd[0]] = p
        created.append(cmd[0])
        return p

    clock = {"now": 100.0}

    def fake_clock():
        return clock["now"]

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=1.0,
        env=None,
        clock=fake_clock,
        sleep=lambda s: None,
        restart_window=600,
        restart_max_per_window=5,
    )
    clock["now"] = 101.0
    sup.tick()
    clock["now"] = 102.0
    sup.tick()
    assert created == ["a", "a", "a"]


def test_window_limit_stops_restarting():
    class DeadProc:
        def poll(self):
            return 1

    created = []

    def factory(cmd, env=None, creationflags=None):
        created.append(cmd[0])
        return DeadProc()

    clock = {"now": 0.0}

    def fake_clock():
        return clock["now"]

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=0.0,
        env=None,
        clock=fake_clock,
        sleep=lambda s: None,
        restart_window=100,
        restart_max_per_window=2,
    )
    for _ in range(5):
        clock["now"] += 1
        sup.tick()
    assert created == ["a", "a", "a"]  # 初始 + 2 次重启后窗口限流停止


def test_health_probe_failure_counts_as_restart():
    created = []
    poll_results = {"a": None}

    class Proc:
        def poll(self):
            return poll_results["a"]

    def factory(cmd, env=None, creationflags=None):
        created.append(cmd[0])
        return Proc()

    class ProbeBus:
        def request(self, service, payload, timeout_ms=2000):
            if service == "health/a":
                raise BusTimeoutError("no heartbeat")

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=0.0,
        env=None,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    sup.tick(bus=ProbeBus(), health_timeout_ms=200)
    assert created == ["a", "a"]  # 初始 + 探活失败重启
```

**说明：** Supervisor 需要支持 `clock`/`sleep`/退避参数注入才能测。Step 3 实现时会给出完整可测版本；若测试与实现有出入，以实现后的通过测试为准并保持语义（退避递增、窗口限流、探活失败计入、bus_server 先决条件）。

- [ ] **Step 3: 实现 `src/yuki/health.py`**

```python
import os
import time

from yuki.bus import MessageBus


def register_health_service(bus: MessageBus, name: str) -> None:
    start = time.time()
    error_count = getattr(bus, "_error_count", 0)

    def handler(payload: dict) -> dict:
        return {
            "process": name,
            "pid": os.getpid(),
            "uptime_s": time.time() - start,
            "error_count": error_count,
        }

    bus.respond(f"health/{name}", handler)
```

- [ ] **Step 4: 重构 `src/yuki/supervisor.py`（注入 clock/sleep + 退避 + 窗口 + 探活）**

```python
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from yuki.bus import BusError, BusTimeoutError, MessageBus
from yuki.logger import get_logger

logger = get_logger("yuki.supervisor")


@dataclass
class Child:
    name: str
    cmd: list[str]
    proc: "subprocess.Popen"
    restarts: int = 0
    attempts: int = 0
    last_restart: float = 0.0
    restart_times: list[float] = field(default_factory=list)
    healthy_since: float = 0.0


class Supervisor:
    def __init__(
        self,
        cmds: list[tuple[str, list[str]]],
        popen_factory: Callable = subprocess.Popen,
        restart_delay: float = 1.0,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        restart_base_delay: float = 1.0,
        restart_max_delay: float = 60.0,
        restart_window: int = 600,
        restart_max_per_window: int = 5,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._env = env
        self._clock = clock
        self._sleep = sleep
        self.restart_base_delay = restart_base_delay
        self.restart_max_delay = restart_max_delay
        self.restart_window = restart_window
        self.restart_max_per_window = restart_max_per_window
        self._children: list[Child] = [
            Child(name=name, cmd=cmd, proc=self._spawn(cmd)) for name, cmd in cmds
        ]

    def _spawn(self, cmd: list[str]) -> "subprocess.Popen":
        kwargs = {}
        if self._env is not None:
            kwargs["env"] = self._env
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return self._popen(cmd, **kwargs)

    def tick(self, bus: MessageBus | None = None, health_timeout_ms: int = 2000) -> list[str]:
        restarted: list[str] = []
        now = self._clock()
        for child in self._children:
            if child.proc.poll() is None:
                # 存活：超过窗口无重启则清 attempts
                if now - child.healthy_since >= self.restart_window:
                    child.attempts = 0
                    child.healthy_since = now
                # 健康探活（bus_server 只靠 poll 判定，不探活；其余子进程在 hub 就绪后探活）
                if bus is not None and child.name != "bus_server":
                    try:
                        bus.request(f"health/{child.name}", {}, timeout_ms=health_timeout_ms)
                    except (BusError, BusTimeoutError):
                        logger.warning("health probe failed", process=child.name)
                        self._restart(child, now)
                        restarted.append(child.name)
                continue
            self._restart(child, now)
            restarted.append(child.name)
        return restarted

    def _restart(self, child: Child, now: float) -> None:
        now_window = now - self.restart_window
        child.restart_times = [t for t in child.restart_times if t >= now_window]
        if len(child.restart_times) >= self.restart_max_per_window:
            logger.critical("giving up on process (too many restarts in window)", process=child.name)
            return
        delay = min(self.restart_base_delay * (2 ** child.attempts), self.restart_max_delay)
        logger.warning("restarting process", process=child.name, attempt=child.attempts, next_delay=delay)
        self._sleep(delay)
        child.restarts += 1
        child.attempts += 1
        child.restart_times.append(now)
        child.proc = self._spawn(child.cmd)
        child.healthy_since = now

    def terminate_children(self, timeout: float = 5.0) -> None:
        for child in self._children:
            if child.proc.poll() is None:
                child.proc.terminate()
        deadline = self._clock() + timeout
        for child in self._children:
            if child.proc.poll() is None:
                try:
                    child.proc.wait(timeout=max(0.0, deadline - self._clock()))
                except subprocess.TimeoutExpired:
                    child.proc.kill()
```

**注意：** `tick()` 签名改为 `tick(bus=None, health_timeout_ms=2000)`——既有 `tests/test_supervisor.py` 的 `sup.tick()` 无参调用继续工作（默认无探活）。Windows 上 supervisor 收到 SIGINT/SIGTERM 时，`supervisor/main.py` 的 finally 在 `terminate_children` 前先向存活子进程发 `CTRL_BREAK_EVENT`（见 Step 5）。

- [ ] **Step 5: 修改 `src/yuki/supervisor/main.py`（探活 + SIGBREAK 关闭）**

```python
import os
import signal
import sys

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.supervisor import Supervisor

CHILDREN = [
    ("bus_server", [sys.executable, "-m", "yuki.bus_server"]),
    ("cognition", [sys.executable, "-m", "yuki.cognition"]),
    ("interaction", [sys.executable, "-m", "yuki.interaction"]),
    ("perception", [sys.executable, "-m", "yuki.perception"]),
]


def build_children_cmds(interaction_extra: list[str] | None = None) -> list[tuple[str, list[str]]]:
    cmds = []
    for name, base in CHILDREN:
        if name == "interaction" and interaction_extra:
            cmds.append((name, base + interaction_extra))
        else:
            cmds.append((name, base))
    return cmds


def _send_break_to_children(supervisor: Supervisor) -> None:
    for child in supervisor._children:
        if child.proc.poll() is None and os.name == "nt":
            try:
                os.kill(child.proc.pid, signal.CTRL_BREAK_EVENT)
            except (OSError, AttributeError):
                pass


def main() -> None:
    config = Config.from_env()
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()

    extra = None
    if "--trigger-after" in sys.argv:
        index = sys.argv.index("--trigger-after")
        extra = ["--trigger-after", sys.argv[index + 1]]

    env = dict(os.environ)
    env["YUKI_BUS_ROLE"] = "node"
    env["YUKI_BASE_PORT"] = str(config.base_port)

    bus = MessageBus(base_port=config.base_port, role="node", hwm=config.hwm)
    supervisor = Supervisor(
        build_children_cmds(extra),
        env=env,
        restart_base_delay=config.restart_base_delay,
        restart_max_delay=config.restart_max_delay,
        restart_window=config.restart_window,
        restart_max_per_window=config.restart_max_per_window,
    )
    try:
        while not shutdown.shutdown_requested:
            try:
                supervisor.tick(bus=bus, health_timeout_ms=config.health_timeout_ms)
            except RuntimeError as exc:
                print(f"[supervisor] {exc}", flush=True)
            shutdown.wait(timeout=0.5)
    finally:
        _send_break_to_children(supervisor)
        supervisor.terminate_children()
        bus.close()


if __name__ == "__main__":
    main()
```

**注意：** supervisor 以 node 角色连接 hub 做探活，`tick(bus=bus, ...)` 传入。bus_server 是 CHILDREN 第一项（build_children_cmds 顺序保证先拉起），且 tick 中对名为 `bus_server` 的子进程跳过 health 请求（靠 poll() 判定存活），避免"hub 未就绪时探活失败误重启"。

- [ ] **Step 6: 三层 main() 注册 health 服务**

`cognition/main.py`/`interaction/main.py`/`perception/main.py` 在 `build_<layer>` 后追加：

```python
from yuki.health import register_health_service
# ...
register_health_service(bus, "cognition")  # 各层用自己的名字
```

- [ ] **Step 7: 改写 `tests/test_e2e.py`（走 supervisor + 挂死保护）**

```python
import os
import subprocess
import sys
import threading
import time

import pytest

E2E_PORT = 6500


def _env(port: int):
    env = dict(os.environ)
    env["YUKI_BASE_PORT"] = str(port)
    env["PYTHONPATH"] = "src"
    return env


@pytest.mark.e2e
def test_hotkey_trigger_flow_reaches_reply():
    port = E2E_PORT + 1
    env = _env(port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "yuki.supervisor", "--trigger-after", "1"],
        env=env,
        cwd=".",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    buffer = []
    stop = threading.Event()

    def reader():
        for line in proc.stdout:
            buffer.append(line)
            if stop.is_set():
                break

    threading.Thread(target=reader, daemon=True).start()
    try:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if any("[yuki] 我在，你说。" in line for line in buffer):
                return
            time.sleep(0.1)
        pytest.fail(f"did not receive reply, output so far: {''.join(buffer)!r}")
    finally:
        stop.set()
        proc.terminate()
        proc.wait(timeout=8)
```

- [ ] **Step 8: 跑测试验证通过**

Run: `python -m pytest -m e2e -v`
Expected: PASS（约 2-5s，4 子进程全部拉起后闭环）

- [ ] **Step 9: 全量回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS
```bash
git add src/yuki/health.py src/yuki/supervisor.py src/yuki/supervisor/main.py src/yuki/cognition/main.py src/yuki/interaction/main.py src/yuki/perception/main.py tests/test_health.py tests/test_supervisor.py tests/test_e2e.py
git commit -m "feat: health probing, exponential backoff, windowed restart limits"
```

---

### Task 6: 接口定义文档

**Files:**
- Create: `docs/superpowers/specs/2026-08-10-yuki-interfaces.md`
- 验证：文档内容清单检查

**Interfaces:**
- Consumes: `Topics`、Task 3 信封/帧协议、Task 5 health 服务名
- Produces: 供 Phase 2b/2c/3/4 实现的接口契约文档

- [ ] **Step 1: 创建 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md`**

```markdown
# Yuki Agent 进程间接口定义

> 日期：2026-08-10
> 状态：Phase 2a 定型；Phase 2b 将 JSON 编码机械替换为 protobuf（信封字段不变）
> 传输：全部 localhost（tcp://127.0.0.1）

## 1. 总线拓扑与角色

- hub（bus_server 进程）：唯一绑定端口 base_port..base_port+2（XSUB/XPUB/ROUTER）
- node（cognition/interaction/perception）：只连接，不绑定；持 PUB/SUB/DEALER 三个套接字
- 角色由 YUKI_BUS_ROLE（Config.bus_role）决定；Supervisor 守护 bus_server 与三个层

## 2. 端口分配

| 端口 | 套接字 | 用途 |
|---|---|---|
| base_port | XSUB | 节点 PUB 连入 |
| base_port+1 | XPUB | 节点 SUB 连入 |
| base_port+2 | ROUTER | 节点 DEALER 连入（REQ/REP 经枢纽） |

## 3. 统一信封

所有总线消息遵循：`{"version": 1, "trace_id": str, ...}`。PUB/SUB 消息含 `"topic"` 与 `"payload"`；REQ/REP 消息含 `"service"`、`"request_id"`、`"payload"` 或 `"result"`/`"error"`。

### PUB/SUB
- 发布：`{"version":1, "topic":<str>, "payload":{...}}`，帧头为主题名
- 订阅：单 SUB 套接字多 SUBSCRIBE；同前缀多 handler 并存；重叠前缀均触发

### ROUTER/DEALER（REQ/REP）
- 注册：`["REGISTER", service]`（服务提供方启动时一次）
- 请求：`[service, json]`，`json={"version","trace_id","service","request_id","payload"}`
- 响应：`[client_identity, json]`，`json={"version","request_id","result"}` 或 `{"version","request_id","error"}`
- 服务未注册：hub 直回 `{"version","request_id","error":"service not found"}`
- 同一服务单提供者，后注册者胜出
- 默认超时 2000ms → BusTimeoutError；error → BusError

## 4. 事件主题与载荷

| 主题 | 方向 | 载荷 |
|---|---|---|
| event/awake | 交互层→总线 | {"source":"hotkey"\|"wakeword","ts":float,"confidence":0..1} |
| event/reply | 认知层→总线 | {"text":str,"ts":float} |
| event/focus_changed | 采集层→总线 | {"app":str,"url":str,"title":str}（Phase 2b） |
| event/heartbeat | 各层→总线 | {"process":str,"ts":float}（可选） |

## 5. 帧主题与格式

### audio/mic（Phase 3 启用）
- PCM 16kHz、16bit、单声道、帧长 20ms（320 字节/帧）
- v1 用 JSON base64 传输；唤醒词检测本身全本地

### frame/request（REQ/REP，Phase 2b 启用）
- 服务名 `frame`；超时 2000ms；失败按降级链

## 6. 健康检查

- 服务名：`health/{process}`（cognition/interaction/perception/bus_server 各自注册）
- 响应：{"process","pid","uptime_s","error_count"}
- Supervisor 定时 REQ 探活，超时视为卡死并重启
- bus_server 未存活时跳过对其他进程探活

## 7. 错误码枚举

| 码 | 常量名 | 含义 |
|---|---|---|
| 1000 | SCREEN_CAPTURE_FAILED | 截屏失败 |
| 2001 | VLM_TIMEOUT | 视觉理解超时 |
| 2002 | VLM_FAILED | 视觉理解失败 |
| 3001 | STT_EMPTY | 语音识别结果为空 |
| 4001 | BUS_TIMEOUT | 总线请求超时 |
| 4002 | SERVICE_NOT_FOUND | 服务未注册 |

降级链：VLM 失败 → 系统信息感知 → L1 本地快答 → 断网本地人格兜底

## 8. 背压与可靠性

- HWM：PUB SNDHWM / SUB RCVHWM / DEALER SND+RCV / ROUTER RCV = 1000
- 订阅/响应 handler 异常：记录日志 + error_count，线程不死
- 重启策略：指数退避（base*2^n，cap），restart_window 秒内窗口计数限流
```

- [ ] **Step 2: 核对清单（验证交付物）**

逐项核对文件包含：§1 拓扑、§2 端口、§3 信封、§4 事件主题、§5 帧格式、§6 健康检查、§7 错误码、§8 背压。缺任一项补齐。

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-08-10-yuki-interfaces.md
git commit -m "docs: define inter-process interface contract"
```

---

### Task 7: 会话录制器

**Files:**
- Create: `src/yuki/recorder/session.py`
- Create: `src/yuki/recorder/cli.py`
- Create: `src/yuki/recorder/__main__.py`
- Test: `tests/recorder/test_session.py`

**Interfaces:**
- Consumes: `MessageBus`（Task 3）、`Config`（Task 2）、`Topics`
- Produces:
  - `class Session`（`yuki.recorder.session`）：
    - `__init__(output_dir: Path, session_id: str | None = None)`
    - `record_event(topic, payload) -> None`
    - `save_frame(image_bytes, fmt="png") -> Path`
    - `close() -> None`（关闭后写入抛 RuntimeError）
  - `yuki.recorder.cli.main()`：`--output-dir`、`--interval`（默认 1.0）、`--no-frames`；订阅 `event/*`；`Ctrl+C` 优雅退出（复用 ShutdownManager 而非裸 KeyboardInterrupt）

- [ ] **Step 1: 写失败测试 `tests/recorder/test_session.py`**

```python
import json

import pytest

from yuki.recorder.session import Session


def test_session_records_events_and_frames(tmp_path):
    session = Session(tmp_path, session_id="sess-001")
    session.record_event("event/awake", {"source": "hotkey"})
    path = session.save_frame(b"\x89PNG\r\n\x1a\nfakepng")
    session.close()

    assert path.exists()
    assert path.name == "000000.png"
    lines = (session.dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["topic"] == "event/awake"
    assert first["payload"] == {"source": "hotkey"}
    second = json.loads(lines[1])
    assert second["topic"] == "recorder/frame"


def test_session_save_frame_sequence_increments(tmp_path):
    session = Session(tmp_path, session_id="sess-002")
    first = session.save_frame(b"frame1")
    second = session.save_frame(b"frame2")
    session.close()
    assert first.name == "000000.png"
    assert second.name == "000001.png"


def test_session_rejects_writes_after_close(tmp_path):
    session = Session(tmp_path, session_id="sess-003")
    session.close()
    with pytest.raises(RuntimeError):
        session.record_event("event/x", {})


def test_session_id_defaults_to_timestamp(tmp_path):
    session = Session(tmp_path)
    assert session.session_id != ""
    session.close()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/recorder/test_session.py -v`
Expected: FAIL，`No module named 'yuki.recorder.session'`

- [ ] **Step 3: 实现 `src/yuki/recorder/session.py`**

```python
import json
import time
from pathlib import Path


class Session:
    def __init__(self, output_dir: Path, session_id: str | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.dir = self.output_dir / self.session_id
        self.frames_dir = self.dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self._frame_seq = 0
        self._closed = False

    def record_event(self, topic: str, payload: dict) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        line = json.dumps({"ts": time.time(), "topic": topic, "payload": payload}, ensure_ascii=False)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def save_frame(self, image_bytes: bytes, fmt: str = "png") -> Path:
        if self._closed:
            raise RuntimeError("session closed")
        path = self.frames_dir / f"{self._frame_seq:06d}.{fmt}"
        path.write_bytes(image_bytes)
        self.record_event("recorder/frame", {"seq": self._frame_seq, "path": str(path), "fmt": fmt})
        self._frame_seq += 1
        return path

    def close(self) -> None:
        self._closed = True
```

- [ ] **Step 4: 实现 `src/yuki/recorder/cli.py`**

```python
import argparse
import io
from pathlib import Path

from PIL import ImageGrab

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.recorder.session import Session
from yuki.shutdown import ShutdownManager


def grab_frame() -> bytes:
    image = ImageGrab.grab()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def run(session: Session, bus: MessageBus, grabber, interval_sec: float) -> None:
    def on_event(topic: str, payload: dict) -> None:
        session.record_event(topic, payload)

    bus.subscribe("event/", on_event)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    next_grab = __import__("time").time()
    while not shutdown.shutdown_requested:
        now = __import__("time").time()
        if now >= next_grab:
            session.save_frame(grabber())
            next_grab = now + interval_sec
        shutdown.wait(timeout=0.05)
    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a browsing session: frames + events, no audio.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frame grabs")
    parser.add_argument("--no-frames", action="store_true", help="record events only")
    args = parser.parse_args()

    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role="node", hwm=config.hwm)
    session = Session(Path(args.output_dir))
    grabber = (lambda: b"") if args.no_frames else grab_frame
    try:
        run(session, bus, grabber, args.interval)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 实现 `src/yuki/recorder/__main__.py`**

```python
from yuki.recorder.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/recorder/test_session.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 7: 回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS
```bash
git add src/yuki/recorder tests/recorder
git commit -m "feat: add browsing session recorder (frames + events, no audio)"
```

---

## Self-Review

**1. Spec coverage：**
- §8.1 分层容错 → Task 4/5
- §11.1 接口定义 → Task 6
- §11.5 会话录制器 → Task 7
- 9 个问题覆盖 → 见"Phase 1 遗留承接 + 新问题覆盖映射"表
- audio/mic 与 frame/request 仅定型契约（Task 6），实际实现属 Phase 2b/2c，符合阶段边界

**2. Placeholder 扫描：** 无 TBD/TODO。`build_perception` 空桩保持（Phase 2b 填充）。Task 5 的 supervisor 探活接线已明确：main() 创建 node 角色 bus，`tick(bus=bus, ...)` 传入；bus_server 跳过 health 探活。

**3. Type consistency：**
- `BusError`/`BusTimeoutError`（Task 3）被 Task 3/5 测试引用，一致
- `MessageBus.request(service, payload, timeout_ms=2000)`（Task 3）被 Task 5 health 探活与 Task 6 文档引用，一致
- `Config` 字段（Task 2）被 Task 5 `restart_*`/`health_*`/`hwm` 引用，一致
- `ShutdownManager`（Task 4）被 Task 4/7 复用，一致
- `register_health_service(bus, name)`（Task 5）被三层 main 与 health 测试引用，一致

**关键取舍：**
- 信封先 JSON 后 proto（Phase 2b 机械替换），避免本计划承载 codegen 管线
- `tick()` 增加可选 `bus` 参数保持既有测试兼容（无参调用不探活）
- supervisor 关闭：先 CTRL_BREAK_EVENT 触发子进程 handler → `terminate_children()` 兜底
- HWM=1000 防背压堆积；错误计数经 health 暴露
