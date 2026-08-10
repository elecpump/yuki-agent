# Yuki Agent 骨架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 Yuki 陪伴 agent 的三进程架构骨架 + 本地消息总线，跑通一个最小可用的"热键呼叫 → 认知层回应 → 交互层输出"闭环，为后续采集/认知/交互功能提供可测试的基座。

**Architecture:** 三进程分层（采集层 Perception / 认知层 Cognition / 交互层 Interaction）+ 发布-订阅消息总线（ZMQ PUB/SUB + REQ/REP，localhost）。本 Phase 只实现骨架：认知层用一个固定的 L1 回应函数，交互层热键用可测试的 HotkeyManager 桩，采集层为空桩进程，Supervisor 负责拉起与重启。

**Tech Stack:** Python ≥3.11，pyzmq（消息总线），pytest（测试），src 布局。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-agent-design.md` 第 2 节（架构）、第 3 节（组件）、第 4.2/4.3 节（数据流）、第 8.1 节（分层容错）。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 进程模型：三进程相互独立，任一崩溃不影响其他层，Supervisor 自动重启
- 消息总线走 localhost（`tcp://127.0.0.1`），绝不跨机器
- 主题命名：`event/awake`（唤醒）、`event/reply`（回应）、`event/*`（感知事件）、`audio/mic`、`audio/tts_ref`（Phase 2/4 启用）
- 音频与原始帧绝不进入本 Phase；唤醒词路径零上传
- 目录结构：`src/yuki/<layer>/`，测试在 `tests/<layer>/`
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交

---

## File Structure

```
pyproject.toml
.gitignore
README.md
src/yuki/__init__.py
src/yuki/bus.py            # MessageBus: publish/subscribe/request/respond
src/yuki/config.py         # Config: 从环境变量读取（端口等）
src/yuki/topics.py         # 主题常量 Topics
src/yuki/cognition/responder.py   # make_reply: L1 回应逻辑（纯函数）
src/yuki/cognition/main.py        # 认知层进程入口
src/yuki/interaction/hotkey.py    # HotkeyManager: 注册/触发
src/yuki/interaction/main.py      # 交互层进程入口
src/yuki/perception/main.py       # 采集层进程入口（空桩）
src/yuki/supervisor.py            # Supervisor: 拉起 + 监控重启
tests/test_bus.py
tests/test_config.py
tests/test_topics.py
tests/test_responder.py
tests/cognition/test_cognition.py
tests/interaction/test_interaction.py
tests/test_supervisor.py
tests/test_e2e.py
```

---

### Task 1: 项目脚手架

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/yuki/__init__.py`
- Create: `tests/test_smoke.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: 无
- Produces: 可 `pip install -e .`、可运行 pytest 的 Python 项目骨架；`src` 布局

- [ ] **Step 1: 初始化 git 仓库与目录**

```bash
git init
mkdir -p src/yuki/cognition src/yuki/interaction src/yuki/perception tests/cognition tests/interaction
```

- [ ] **Step 2: 写 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "yuki"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pyzmq>=25"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: 写 `.gitignore`**

```
__pycache__/
*.egg-info/
.pytest_cache/
build/
dist/
.venv/
```

- [ ] **Step 4: 写 `src/yuki/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 5: 写失败测试 `tests/test_smoke.py`**

```python
import yuki


def test_version():
    assert yuki.__version__ == "0.1.0"
```

- [ ] **Step 6: 安装并跑测试**

Run: `python -m pip install -e ".[dev]"`
Run: `python -m pytest -v`
Expected: `test_version` PASS，项目可导入。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "chore: scaffold yuki project with src layout"
```

---

### Task 2: 消息总线 MessageBus

**Files:**
- Create: `src/yuki/bus.py`
- Test: `tests/test_bus.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class MessageBus`
    - `__init__(self, base_port: int = 5555)`
    - `publish(self, topic: str, payload: dict) -> None`
    - `subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None`
    - `request(self, service: str, payload: dict) -> dict`
    - `respond(self, service: str, handler: Callable[[dict], dict]) -> None`

- [ ] **Step 1: 写失败测试 `tests/test_bus.py`**

```python
import threading
import time

import pytest

from yuki.bus import MessageBus


def _wait_sub(t=0.05):
    time.sleep(t)  # ZMQ PUB/SUB slow-joiner 保护


def test_publish_subscribe_roundtrip():
    bus = MessageBus(base_port=6001)
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


def test_subscribe_filters_by_prefix():
    bus = MessageBus(base_port=6002)
    hits = []

    def on_awake(topic, payload):
        hits.append(payload)

    bus.subscribe("event/awake", on_awake)
    _wait_sub()
    bus.publish("event/reply", {"text": "hi"})
    bus.publish("event/awake", {"source": "hotkey"})
    time.sleep(0.3)
    assert hits == [{"source": "hotkey"}]


def test_request_respond_roundtrip():
    bus = MessageBus(base_port=6003)

    def handler(payload):
        return {"echo": payload["msg"]}

    bus.respond("ping", handler)
    time.sleep(0.05)
    result = bus.request("ping", {"msg": "hello"})
    assert result == {"echo": "hello"}
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_bus.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'yuki.bus'`

- [ ] **Step 3: 实现 `src/yuki/bus.py`**

```python
import json
import threading
from typing import Callable

import zmq


class MessageBus:
    """本地消息总线：PUB/SUB 事件 + REQ/REP 服务调用，仅 localhost。"""

    def __init__(self, base_port: int = 5555):
        self._ctx = zmq.Context()
        self._pub_port = base_port
        self._rep_port = base_port + 1
        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.bind(f"tcp://127.0.0.1:{self._pub_port}")
        self._handlers: dict[str, Callable[[str, dict], None]] = {}
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict) -> None:
        self._pub.send_multipart([topic.encode(), json.dumps(payload).encode()])

    def subscribe(self, topic_prefix: str, handler: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._handlers[topic_prefix] = handler
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(f"tcp://127.0.0.1:{self._pub_port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, topic_prefix)
        thread = threading.Thread(target=self._run_sub, args=(sub,), daemon=True)
        thread.start()

    def _run_sub(self, sub) -> None:
        while True:
            raw_topic, raw_payload = sub.recv_multipart()
            topic = raw_topic.decode()
            payload = json.loads(raw_payload.decode())
            with self._lock:
                handler = self._find_handler(topic)
            if handler is not None:
                handler(topic, payload)

    def _find_handler(self, topic: str) -> Callable[[str, dict], None] | None:
        for prefix, handler in self._handlers.items():
            if topic.startswith(prefix):
                return handler
        return None

    def request(self, service: str, payload: dict) -> dict:
        req = self._ctx.socket(zmq.REQ)
        req.connect(f"tcp://127.0.0.1:{self._rep_port}")
        req.send_json({"service": service, "payload": payload})
        result = req.recv_json()
        req.close()
        return result

    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        rep = self._ctx.socket(zmq.REP)
        rep.bind(f"tcp://127.0.0.1:{self._rep_port}")

        def loop() -> None:
            while True:
                msg = rep.recv_json()
                if msg["service"] == service:
                    result = handler(msg["payload"])
                    rep.send_json({"ok": True, "result": result})

        threading.Thread(target=loop, daemon=True).start()
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_bus.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/bus.py tests/test_bus.py
git commit -m "feat: add localhost publish-subscribe message bus"
```

---

### Task 3: 配置 Config

**Files:**
- Create: `src/yuki/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `@dataclass class Config`
    - `base_port: int = 5555`
    - `log_level: str = "INFO"`
    - `persona_name: str = "yuki"`
    - `@classmethod from_env(cls) -> "Config"`：读取环境变量 `YUKI_BASE_PORT` / `YUKI_LOG_LEVEL` / `YUKI_PERSONA_NAME`

- [ ] **Step 1: 写失败测试 `tests/test_config.py`**

```python
from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.base_port == 5555
    assert config.log_level == "INFO"
    assert config.persona_name == "yuki"


def test_from_env(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("YUKI_PERSONA_NAME", "aki")
    config = Config.from_env()
    assert config.base_port == 7000
    assert config.log_level == "DEBUG"
    assert config.persona_name == "aki"


def test_from_env_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("YUKI_BASE_PORT", raising=False)
    monkeypatch.delenv("YUKI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("YUKI_PERSONA_NAME", raising=False)
    config = Config.from_env()
    assert config.base_port == 5555
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL，`No module named 'yuki.config'`

- [ ] **Step 3: 实现 `src/yuki/config.py`**

```python
import os
from dataclasses import dataclass


@dataclass
class Config:
    base_port: int = 5555
    log_level: str = "INFO"
    persona_name: str = "yuki"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_port=int(os.environ.get("YUKI_BASE_PORT", "5555")),
            log_level=os.environ.get("YUKI_LOG_LEVEL", "INFO"),
            persona_name=os.environ.get("YUKI_PERSONA_NAME", "yuki"),
        )
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/config.py tests/test_config.py
git commit -m "feat: add environment-driven config"
```

---

### Task 4: 主题常量 Topics

**Files:**
- Create: `src/yuki/topics.py`
- Test: `tests/test_topics.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class Topics`（常量命名空间）：
    - `AWAKE = "event/awake"`
    - `REPLY = "event/reply"`
    - `FOCUS_CHANGED = "event/focus_changed"`
    - `MIC = "audio/mic"`
    - `TTS_REF = "audio/tts_ref"`

- [ ] **Step 1: 写失败测试 `tests/test_topics.py`**

```python
from yuki.topics import Topics


def test_topic_constants():
    assert Topics.AWAKE == "event/awake"
    assert Topics.REPLY == "event/reply"
    assert Topics.FOCUS_CHANGED == "event/focus_changed"
    assert Topics.MIC == "audio/mic"
    assert Topics.TTS_REF == "audio/tts_ref"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_topics.py -v`
Expected: FAIL，`No module named 'yuki.topics'`

- [ ] **Step 3: 实现 `src/yuki/topics.py`**

```python
class Topics:
    AWAKE = "event/awake"
    REPLY = "event/reply"
    FOCUS_CHANGED = "event/focus_changed"
    MIC = "audio/mic"
    TTS_REF = "audio/tts_ref"
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_topics.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/topics.py tests/test_topics.py
git commit -m "feat: add bus topic constants"
```

---

### Task 5: L1 回应逻辑 responder

**Files:**
- Create: `src/yuki/cognition/responder.py`
- Test: `tests/test_responder.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `def make_reply(awake: dict) -> dict`：接收唤醒事件 `{"source": str, "ts": float}`，返回 `{"text": str, "ts": float}`。本 Phase 为固定回应文本，Phase 3 替换为真实 L1/L2 引擎。

- [ ] **Step 1: 写失败测试 `tests/test_responder.py`**

```python
import time

from yuki.cognition.responder import make_reply


def test_make_reply_shape():
    now = time.time()
    reply = make_reply({"source": "hotkey", "ts": now})
    assert set(reply.keys()) == {"text", "ts"}
    assert isinstance(reply["text"], str)
    assert reply["ts"] >= now


def test_make_reply_acknowledges_call():
    reply = make_reply({"source": "hotkey", "ts": 0.0})
    assert reply["text"] == "我在，你说。"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_responder.py -v`
Expected: FAIL，`No module named 'yuki.cognition.responder'`

- [ ] **Step 3: 实现 `src/yuki/cognition/responder.py`**

```python
import time


def make_reply(awake: dict) -> dict:
    """对唤醒事件的 L1 回应。Phase 3 由真实引擎替换。"""
    return {"text": "我在，你说。", "ts": time.time()}
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_responder.py -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/cognition/responder.py tests/test_responder.py
git commit -m "feat: add L1 responder stub"
```

---

### Task 6: 认知层进程入口

**Files:**
- Create: `src/yuki/cognition/main.py`
- Test: `tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `MessageBus`（Task 2）、`Topics`（Task 4）、`make_reply`（Task 5）、`Config`（Task 3）
- Produces:
  - `def build_cognition(bus: MessageBus) -> None`：订阅 `event/awake`，收到后发布 `event/reply`（用 `make_reply`）
  - `def main() -> None`：读 `Config.from_env()`，建 `MessageBus`，`build_cognition`，进入事件循环

- [ ] **Step 1: 写失败测试 `tests/cognition/test_cognition.py`**

```python
from yuki.cognition.main import build_cognition
from yuki.topics import Topics


class FakeBus:
    def __init__(self):
        self.handler = None
        self.published = []

    def subscribe(self, prefix, handler):
        self.handler = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


def test_build_cognition_wires_awake_to_reply():
    bus = FakeBus()
    build_cognition(bus)
    assert bus.handler is not None
    bus.handler(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.REPLY
    assert payload["text"] == "我在，你说。"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_cognition.py -v`
Expected: FAIL，`No module named 'yuki.cognition.main'`

- [ ] **Step 3: 实现 `src/yuki/cognition/main.py`**

```python
import time
from typing import Callable

from yuki.bus import MessageBus
from yuki.cognition.responder import make_reply
from yuki.config import Config
from yuki.topics import Topics


def build_cognition(bus: MessageBus) -> None:
    def on_awake(topic: str, payload: dict) -> None:
        bus.publish(Topics.REPLY, make_reply(payload))

    bus.subscribe(Topics.AWAKE, on_awake)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port)
    build_cognition(bus)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_cognition.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/cognition/main.py tests/cognition/test_cognition.py
git commit -m "feat: add cognition process entry"
```

---

### Task 7: 交互层进程入口（热键桩）

**Files:**
- Create: `src/yuki/interaction/hotkey.py`
- Create: `src/yuki/interaction/main.py`
- Test: `tests/interaction/test_interaction.py`

**Interfaces:**
- Consumes: `MessageBus`（Task 2）、`Topics`（Task 4）、`Config`（Task 3）
- Produces:
  - `class HotkeyManager`
    - `register(self, name: str, handler: Callable[[], None]) -> None`
    - `trigger(self, name: str) -> None`（Phase 4 替换为真实全局热键）
  - `def build_interaction(bus: MessageBus, hotkeys: HotkeyManager) -> None`：订阅 `event/reply` 打印；注册 `"trigger"` 处理器发布 `event/awake`
  - `def main() -> None`：读配置、建 bus/hotkeys、`build_interaction`、支持 `--trigger-after <seconds>` 测试钩子、`hotkeys.run()`

- [ ] **Step 1: 写失败测试 `tests/interaction/test_interaction.py`**

```python
from yuki.interaction.hotkey import HotkeyManager
from yuki.interaction.main import build_interaction
from yuki.topics import Topics


class FakeBus:
    def __init__(self):
        self.handler = None
        self.published = []

    def subscribe(self, prefix, handler):
        self.handler = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class FakeHotkeys:
    def __init__(self):
        self.handler = None

    def register(self, name, handler):
        self.handler = handler

    def trigger(self, name):
        self.handler()


def test_hotkey_manager_register_trigger():
    calls = []
    hk = HotkeyManager()
    hk.register("trigger", lambda: calls.append("x"))
    hk.trigger("trigger")
    assert calls == ["x"]


def test_build_interaction_publishes_awake_on_trigger():
    bus = FakeBus()
    hotkeys = FakeHotkeys()
    build_interaction(bus, hotkeys)
    hotkeys.trigger("trigger")
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.AWAKE
    assert payload["source"] == "hotkey"


def test_build_interaction_subscribes_to_reply():
    bus = FakeBus()
    build_interaction(bus, FakeHotkeys())
    assert bus.handler is not None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/interaction/test_interaction.py -v`
Expected: FAIL，`No module named 'yuki.interaction.hotkey'`

- [ ] **Step 3: 实现 `src/yuki/interaction/hotkey.py`**

```python
from typing import Callable


class HotkeyManager:
    """全局热键管理器。Phase 4 接入真实 Windows 全局热键。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[], None]] = {}

    def register(self, name: str, handler: Callable[[], None]) -> None:
        self._handlers[name] = handler

    def trigger(self, name: str) -> None:
        if name in self._handlers:
            self._handlers[name]()
```

- [ ] **Step 4: 实现 `src/yuki/interaction/main.py`**

```python
import sys
import threading
import time

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.interaction.hotkey import HotkeyManager
from yuki.topics import Topics


def build_interaction(bus: MessageBus, hotkeys: HotkeyManager) -> None:
    def on_reply(topic: str, payload: dict) -> None:
        print(f"[yuki] {payload['text']}", flush=True)

    def trigger_call() -> None:
        bus.publish(Topics.AWAKE, {"source": "hotkey", "ts": time.time()})

    bus.subscribe(Topics.REPLY, on_reply)
    hotkeys.register("trigger", trigger_call)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port)
    hotkeys = HotkeyManager()
    build_interaction(bus, hotkeys)

    if "--trigger-after" in sys.argv:
        delay = float(sys.argv[sys.argv.index("--trigger-after") + 1])

        def delayed() -> None:
            time.sleep(delay)
            hotkeys.trigger("trigger")

        threading.Thread(target=delayed, daemon=True).start()

    hotkeys.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/interaction/test_interaction.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 6: 提交**

```bash
git add src/yuki/interaction tests/interaction
git commit -m "feat: add interaction process entry with hotkey stub"
```

---

### Task 8: 采集层进程入口（空桩）

**Files:**
- Create: `src/yuki/perception/main.py`
- Test: `tests/test_perception_smoke.py`

**Interfaces:**
- Consumes: `MessageBus`（Task 2）、`Config`（Task 3）
- Produces:
  - `def build_perception(bus: MessageBus) -> None`：本 Phase 为空实现（Phase 2 填真实采集）
  - `def main() -> None`：读配置、建 bus、`build_perception`、进入事件循环

- [ ] **Step 1: 写失败测试 `tests/test_perception_smoke.py`**

```python
from yuki.perception.main import build_perception


class FakeBus:
    pass


def test_build_perception_is_callable():
    bus = FakeBus()
    result = build_perception(bus)
    assert result is None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_perception_smoke.py -v`
Expected: FAIL，`No module named 'yuki.perception.main'`

- [ ] **Step 3: 实现 `src/yuki/perception/main.py`**

```python
import time

from yuki.bus import MessageBus
from yuki.config import Config


def build_perception(bus: MessageBus) -> None:
    """Phase 2 实现截屏/音频/系统监控。"""


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port)
    build_perception(bus)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_perception_smoke.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/perception/main.py tests/test_perception_smoke.py
git commit -m "feat: add perception process stub"
```

---

### Task 9: Supervisor 看门狗

**Files:**
- Create: `src/yuki/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class Child`：`name: str`, `cmd: list[str]`, `proc`, `restarts: int`
  - `class Supervisor`
    - `__init__(self, cmds: list[tuple[str, list[str]]], popen_factory=subprocess.Popen, restart_delay: float = 1.0)`
    - `tick(self, max_restarts: int = 3) -> list[str]`：一次监控循环，返回本次重启的进程名列表；任一子进程超过 `max_restarts` 次重启则抛 `RuntimeError`
  - 单测用假 `popen_factory` 注入，不真实起子进程

- [ ] **Step 1: 写失败测试 `tests/test_supervisor.py`**

```python
import pytest

from yuki.supervisor import Supervisor


class FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.spawn_count = 0

    def poll(self):
        return self._exit_code


def _fake_factory(procs):
    index = {"n": 0}

    def factory(cmd):
        if cmd[0] == "dead":
            p = procs["dead"]
            p.spawn_count += 1
            return p
        if cmd[0] == "ok":
            p = procs["ok"]
            p.spawn_count += 1
            return p
        raise AssertionError(cmd)

    return factory


def test_supervisor_restarts_dead_process():
    dead = FakeProc(exit_code=1)
    ok = FakeProc(exit_code=None)
    sup = Supervisor(
        [("dead", ["dead"]), ("ok", ["ok"])],
        popen_factory=_fake_factory({"dead": dead, "ok": ok}),
        restart_delay=0.0,
    )
    restarted = sup.tick(max_restarts=3)
    assert restarted == ["dead"]
    assert dead.spawn_count == 2  # 初始 1 次 + 重启 1 次
    assert ok.spawn_count == 1


def test_supervisor_gives_up_after_max_restarts():
    dead = FakeProc(exit_code=1)
    sup = Supervisor(
        [("dead", ["dead"])],
        popen_factory=_fake_factory({"dead": dead, "ok": None}),
        restart_delay=0.0,
    )
    with pytest.raises(RuntimeError):
        for _ in range(10):
            sup.tick(max_restarts=3)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_supervisor.py -v`
Expected: FAIL，`No module named 'yuki.supervisor'`

- [ ] **Step 3: 实现 `src/yuki/supervisor.py`**

```python
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable


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
        popen_factory: Callable[[list[str]], "subprocess.Popen"] = subprocess.Popen,
        restart_delay: float = 1.0,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._children: list[Child] = [
            Child(name=name, cmd=cmd, proc=self._popen(cmd)) for name, cmd in cmds
        ]

    def tick(self, max_restarts: int = 3) -> list[str]:
        restarted: list[str] = []
        for child in self._children:
            if child.proc.poll() is not None:
                if child.restarts >= max_restarts:
                    raise RuntimeError(f"{child.name} crashed too many times")
                child.restarts += 1
                time.sleep(self._restart_delay)
                child.proc = self._popen(child.cmd)
                restarted.append(child.name)
        return restarted
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_supervisor.py -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/supervisor.py tests/test_supervisor.py
git commit -m "feat: add supervisor watchdog with restart limit"
```

---

### Task 10: 端到端集成测试 + README

**Files:**
- Create: `tests/test_e2e.py`
- Create: `README.md`
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: Task 1-9 全部
- Produces: 一条可运行的完整命令：`python -m yuki.interaction --trigger-after 2` 结合认知层与采集层进程，验证热键 → awake → reply 闭环

- [ ] **Step 1: 写失败测试 `tests/test_e2e.py`**

```python
import subprocess
import sys
import time

import pytest

from yuki.config import Config

E2E_PORT = 6500


def _env(port: int):
    env = {
        "YUKI_BASE_PORT": str(port),
        "PYTHONPATH": "src",
    }
    return env


@pytest.mark.e2e
def test_hotkey_trigger_flow_reaches_reply():
    port = E2E_PORT + 1
    env = _env(port)
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "yuki.cognition"], env=env, cwd="."
        ),
        subprocess.Popen(
            [sys.executable, "-m", "yuki.interaction", "--trigger-after", "1"],
            env=env,
            cwd=".",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ),
    ]
    try:
        deadline = time.time() + 8.0
        output = ""
        while time.time() < deadline:
            line = procs[1].stdout.readline()
            if line:
                output += line
                if "[yuki] 我在，你说。" in output:
                    return
            time.sleep(0.1)
        pytest.fail(f"did not receive reply, output so far: {output!r}")
    finally:
        for p in procs:
            p.terminate()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_e2e.py -m e2e -v`
Expected: 超时 FAIL（当前没有认知层进程入口）。

- [ ] **Step 3: 写 `README.md`**

```markdown
# Yuki Agent

Windows 上的纯语音陪伴 agent（开发中）。第一期：浏览/阅读场景感知与陪伴。

## 架构

三进程分层 + 本地消息总线（ZMQ PUB/SUB + REQ/REP）：
- `src/yuki/perception` 采集层（Phase 2 实现）
- `src/yuki/cognition` 认知层
- `src/yuki/interaction` 交互层

## 运行

```bash
pip install -e ".[dev]"
# 终端 1
python -m yuki.cognition
# 终端 2（触发一次呼叫）
python -m yuki.interaction --trigger-after 2
```

## 测试

```bash
pytest                        # 单元测试
pytest -m e2e                 # 端到端集成测试
```

## 文档

- 设计文档：`docs/superpowers/specs/2026-08-10-yuki-agent-design.md`
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_e2e.py -m e2e -v`
Expected: PASS，`readline` 读到 `[yuki] 我在，你说。`

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS
```bash
git add tests/test_e2e.py README.md
git commit -m "test: add end-to-end hotkey loop; docs: README"
```

---

## Self-Review

**1. Spec coverage：**
- 第 2 节架构 → Task 1-10（三进程 + 总线 + Supervisor）
- 第 2.2 主题命名 → Task 4（Topics 常量含 `event/awake`、`event/reply`）
- 第 4.2 被动回应闭环 → Task 6/7/10（awake → make_reply → reply）
- 第 8.1 分层容错 → Task 9（Supervisor 重启与上限）
- 音频/敏感内容/云端 → 本 Phase 不涉及（后续 Phase 覆盖），无缺口

**2. Placeholder 扫描：** `build_perception` 的空函数体是刻意的最小桩（有测试断言其可调用），有 Phase 2 承接；无 TBD/TODO。

**3. Type consistency：** `make_reply(dict) -> dict`、`MessageBus.publish/subscribe` 签名在 Task 2/5/6/7 间一致；`Supervisor.tick -> list[str]` 在 Task 9 内自洽；`Config.from_env()` 在 Task 3/6/7/8 一致。

**已知取舍：** ZMQ PUB/SUB 有 slow-joiner 窗口，单测用 `_wait_sub()` 和固定 sleep 缓解；e2e 测试用 `--trigger-after` 测试钩子代替真实全局热键（真实热键在 Phase 4）。
