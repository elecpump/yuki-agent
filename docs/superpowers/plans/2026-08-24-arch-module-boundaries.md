# 模块边界与职责 Implementation Plan（架构评审主题 9）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收敛模块边界：`DecisionHub` 经 `DecisionSink` 协议接触 tuner/sedimenter；`perception_tools` 只依赖 `ScreenQueryPort` 协议；`PerceptionPipeline` 暴露公开 `frame_client`/`vlm` 属性消除 `assembly.py` 的私有反射；ASR 状态机从 `PerceptionPipeline` 拆出为独立 `AsrSession`。

**Architecture:** 四个独立重构，每个可独立交付：
1. `src/yuki/cognition/brain/sink.py` 定义 `DecisionSink` Protocol（on_proactive_open/on_user_utterance/on_engagement），hub 持有 `sinks: list[DecisionSink]`，tuner/sedimenter 经适配器注册；行为等价。
2. `ScreenQueryPort` Protocol 定义于 `functions/screen.py`，`perception_tools.py` 只依赖协议（PerceptionPipeline 结构上满足）。
3. `PerceptionPipeline` 增加 `@property frame_client`/`@property vlm`，`assembly.py` 走公开 API。
4. `src/yuki/cognition/asr_session.py` 抽出 ASR 状态机（`_asr_state/_pre_roll/_speech_buffer/check_asr_due/on_awake/on_mic`），pipeline 委托。

**Tech Stack:** Python ≥3.11（Protocol），pytest。无新增运行时依赖。

## Global Constraints

- 每个任务都是**行为等价重构**：现有 test_hub.py、test_perception_tools.py、test_assembly.py、test_pipeline.py 全部保持通过（除明确改写的断言）。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。
- 任务间解耦：Task 1/2/3/4 可按任意顺序执行，各改各的文件。
- `DecisionSink` 语义：`on_proactive_open()`、`on_user_utterance(text)`、`on_engagement(topic)`；sedimenter 的 intent 过滤逻辑（trusted_metadata 门）保留在 hub 内，见 Task 1 Step 3 说明。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/sink.py` — `DecisionSink` Protocol + TunerSink/SedimenterSink 适配器
- `src/yuki/functions/screen.py` — `ScreenQueryPort` Protocol
- `src/yuki/cognition/asr_session.py` — `AsrSession` 独立类
- `tests/cognition/test_sink.py`、`tests/cognition/test_asr_session.py`

**修改**
- `src/yuki/cognition/brain/hub.py` — sinks 列表接入
- `src/yuki/functions/perception_tools.py` — 依赖 `ScreenQueryPort`
- `src/yuki/cognition/pipeline.py` — 公开属性 + 委托 AsrSession
- `src/yuki/cognition/assembly.py` — 走公开 API
- 测试：`test_hub.py`、`test_perception_tools.py`、`test_assembly.py`、`test_pipeline.py`

---

### Task 1: DecisionSink 协议解耦 tuner/sedimenter

**Files:**
- Create: `src/yuki/cognition/brain/sink.py`
- Create: `tests/cognition/test_sink.py`
- Modify: `src/yuki/cognition/brain/hub.py`
- Modify: `tests/cognition/test_hub.py`

**Interfaces:**
- Consumes: `FeedbackTuner`、`PreferenceSedimenter` 的现有方法签名。
- Produces: `DecisionSink` Protocol（`on_proactive_open()`/`on_user_utterance(text)`/`on_engagement(topic)`）；`TunerSink(tuner)`、`SedimenterSink(sedimenter)` 适配器；`DecisionHub.register_sink(sink)`。hub 经 `self._sinks` 调三个回调。

- [ ] **Step 1: 创建 `tests/cognition/test_sink.py`（先红）**

```python
import pytest

from yuki.cognition.brain.sink import SedimenterSink, TunerSink


class FakeTuner:
    def __init__(self):
        self.calls = []

    def on_proactive_open(self):
        self.calls.append("open")

    def on_user_utterance(self, text):
        self.calls.append(("utter", text))


class FakeSedimenter:
    def __init__(self):
        self.calls = []

    def on_user_utterance(self, text, intent):
        self.calls.append(("utter", text, intent))

    def on_engagement(self, topic):
        self.calls.append(("engage", topic))


def test_tuner_sink_forwards_proactive_and_utterance():
    tuner = FakeTuner()
    sink = TunerSink(tuner)
    sink.on_proactive_open()
    sink.on_user_utterance("你好")
    assert tuner.calls == ["open", ("utter", "你好")]


def test_sedimenter_sink_forwards_utterance_with_intent_and_engagement():
    sed = FakeSedimenter()
    sink = SedimenterSink(sed)
    sink.on_user_utterance("真好")
    sink.on_engagement("量子计算")
    assert sed.calls == [("utter", "真好", "any"), ("engage", "量子计算")]
```

注：`SedimenterSink.on_user_utterance` 无 intent 参数（hub 侧已过滤可信 metadata），适配器内部传默认 intent 占位；最终实现按 Step 3 对齐 `PreferenceSedimenter` 实际签名。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_sink.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.sink'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/sink.py` 并接入 hub**

`src/yuki/cognition/brain/sink.py`：

```python
from typing import Protocol


class DecisionSink(Protocol):
    """hub 决策事件的下游消费者（tuner/sedimenter 等）。"""

    def on_proactive_open(self) -> None: ...

    def on_user_utterance(self, text: str) -> None: ...

    def on_engagement(self, topic: str) -> None: ...


class TunerSink:
    def __init__(self, tuner) -> None:
        self._tuner = tuner

    def on_proactive_open(self) -> None:
        self._tuner.on_proactive_open()

    def on_user_utterance(self, text: str) -> None:
        self._tuner.on_user_utterance(text)

    def on_engagement(self, topic: str) -> None:
        pass


class SedimenterSink:
    def __init__(self, sedimenter) -> None:
        self._sedimenter = sedimenter

    def on_proactive_open(self) -> None:
        pass

    def on_user_utterance(self, text: str) -> None:
        # hub 已保证仅可信 router metadata 触发；此处按 sedimenter 约定以默认 intent 落库
        self._sedimenter.on_user_utterance(text, "any")

    def on_engagement(self, topic: str) -> None:
        self._sedimenter.on_engagement(topic)
```

`src/yuki/cognition/brain/hub.py`：
- import 区新增：`from yuki.cognition.brain.sink import DecisionSink, SedimenterSink, TunerSink`
- `__init__` 中保留 `self._tuner`/`self._sedimenter` 原赋值（兼容旧测试），新增：

```python
        self._sinks: list[DecisionSink] = []
        if tuner is not None:
            self._sinks.append(TunerSink(tuner))
        if sedimenter is not None:
            self._sinks.append(SedimenterSink(sedimenter))
```

- 新增公开方法：

```python
    def register_sink(self, sink: DecisionSink) -> None:
        self._sinks.append(sink)
```

- `_handle_locked` 中把 tuner/sedimenter 直调改为 sink 遍历，**保留原有 trusted_metadata 门**：

```python
        if trigger == TriggerKind.UTTERANCE and spoke:
            pass  # 占位：见下方替换
```
原逻辑（hub.py:202-219）替换为：

```python
        if spoke:
            for sink in self._sinks:
                if trigger == TriggerKind.SITUATION:
                    sink.on_proactive_open()

        intent = result["intent"]
        if (
            trigger == TriggerKind.UTTERANCE
            and result.get("trusted_metadata")
            and intent != Intent.UNKNOWN
            and result.get("reason") != "crisis"
        ):
            topic = (effective_situation or {}).get("topic")
            for sink in self._sinks:
                sink.on_user_utterance(text)
                if topic:
                    sink.on_engagement(topic)
```

- [ ] **Step 4: 更新 `tests/cognition/test_hub.py` 的 FakeSedimenter 断言**

原 `FakeSedimenter.on_user_utterance(self, text, intent)` 改为协议签名（hub 只传 text）：

```python
class FakeSedimenter:
    def __init__(self):
        self.utterances = []
        self.topics = []

    def on_user_utterance(self, text, intent=None):
        self.utterances.append((text, intent))

    def on_engagement(self, topic):
        self.topics.append(topic)
```

`test_sedimenter_only_receives_trusted_router_metadata` 断言改为 `("其实我不喜欢主动聊天", None)`（hub 不再传 intent）。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/cognition/test_sink.py tests/cognition/test_hub.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/brain/sink.py src/yuki/cognition/brain/hub.py tests/cognition/test_sink.py tests/cognition/test_hub.py
git commit -m "refactor: decouple DecisionHub from tuner/sedimenter via DecisionSink protocol"
```

---

### Task 2: ScreenQueryPort 依赖反转

**Files:**
- Create: `src/yuki/functions/screen.py`
- Modify: `src/yuki/functions/perception_tools.py`
- Modify: `tests/functions/test_perception_tools.py`

**Interfaces:**
- Consumes: 无。
- Produces: `ScreenQueryPort` Protocol（`latest_frame() -> dict`、`current_text() -> dict`、`understand_screen_deep(*, bypass_rate_limit) -> dict`）。`register_perception_tools(registry, screen: ScreenQueryPort)` 只依赖协议；`PerceptionPipeline` 结构上满足。

- [ ] **Step 1: 创建 `tests/functions/test_perception_tools.py` 前置（先红）**

```python
def test_register_perception_tools_accepts_any_screen_port():
    class FakeScreen:
        def latest_frame(self):
            return {"frame_id": 1}

        def current_text(self):
            return {"text": "hi"}

        def understand_screen_deep(self, *, bypass_rate_limit=None):
            return {"topic": "t"}

    registry = FunctionRegistry()
    register_perception_tools(registry, FakeScreen())
    assert registry.call("screen.capture", None)["frame_id"] == 1
    assert registry.call("text.extract", None)["text"] == "hi"
    assert registry.call("vision.understand", None)["topic"] == "t"
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/functions/test_perception_tools.py -v`
Expected: 当前通过（`PerceptionPipeline` 满足结构）；本任务把类型收紧为协议，运行应仍通过——先红步骤验证协议存在即可：

```python
from yuki.functions.screen import ScreenQueryPort


def test_screen_query_port_protocol_exists():
    assert hasattr(ScreenQueryPort, "latest_frame")
    assert hasattr(ScreenQueryPort, "current_text")
    assert hasattr(ScreenQueryPort, "understand_screen_deep")
```

Run: `python -m pytest tests/functions/test_perception_tools.py -v -k "protocol_exists"`
Expected: FAIL（`ModuleNotFoundError: yuki.functions.screen`）。

- [ ] **Step 3: 创建 `src/yuki/functions/screen.py`**

```python
from typing import Protocol


class ScreenQueryPort(Protocol):
    """perception 读屏能力的最小面：perception_tools 只依赖此协议。"""

    def latest_frame(self) -> dict: ...

    def current_text(self) -> dict: ...

    def understand_screen_deep(self, *, bypass_rate_limit: bool | None = None) -> dict: ...
```

- [ ] **Step 4: 修改 `src/yuki/functions/perception_tools.py`**

- 删除 `from yuki.cognition.pipeline import PerceptionPipeline`，改：

```python
from yuki.functions.screen import ScreenQueryPort
```

- `register_perception_tools` 签名：

```python
def register_perception_tools(
    registry: FunctionRegistry,
    screen: ScreenQueryPort,
    *,
    foreground_probe: ForegroundProbe | None = None,
) -> None:
```

- 函数体内 `pipeline.latest_frame()` → `screen.latest_frame()`、`pipeline.current_text()` → `screen.current_text()`、`pipeline.understand_screen_deep(bypass_rate_limit=True)` → `screen.understand_screen_deep(bypass_rate_limit=True)`。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/functions/test_perception_tools.py tests/cognition/test_assembly.py -v`
Expected: 全 PASS（`PerceptionPipeline` 结构满足协议，注册处 `_register_perception_functions` 传入的 pipeline 无需改）。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/functions/screen.py src/yuki/functions/perception_tools.py tests/functions/test_perception_tools.py
git commit -m "refactor: perception tools depend on ScreenQueryPort protocol only"
```

---

### Task 3: PerceptionPipeline 公开 frame_client/vlm 属性

**Files:**
- Modify: `src/yuki/cognition/pipeline.py`
- Modify: `src/yuki/cognition/assembly.py`
- Modify: `tests/cognition/test_pipeline.py`

**Interfaces:**
- Consumes: 无。
- Produces: `PerceptionPipeline.frame_client`、`PerceptionPipeline.vlm`（只读属性）。`assembly.py::_build_local_brain` 改用公开属性。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_pipeline.py`**

```python
def test_pipeline_exposes_public_frame_client_and_vlm():
    bus = FakeBus()
    vlm = FakeVLM()
    pipeline = build_pipeline(
        bus, vlm=vlm, stt=FakeSTT(), frame_client=FakeFrameClient(),
        start_deep_timer=False, start_asr_watchdog=False,
    )
    try:
        assert pipeline.frame_client is pipeline._frame_client
        assert pipeline.vlm is vlm
    finally:
        pipeline.close()
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_pipeline.py -v -k "public_frame_client"`
Expected: FAIL（`AttributeError: 'PerceptionPipeline' object has no attribute 'frame_client'`）。

- [ ] **Step 3: 修改 `src/yuki/cognition/pipeline.py`**

在类内新增属性（放在 `warmup_vlm` 之前）：

```python
    @property
    def frame_client(self) -> FrameClient:
        return self._frame_client

    @property
    def vlm(self) -> VisualUnderstander:
        return self._vlm
```

- [ ] **Step 4: 修改 `src/yuki/cognition/assembly.py::_build_local_brain`**

`src/yuki/cognition/assembly.py:300-305` 改为：

```python
        frame_client = pipeline.frame_client
        vlm = pipeline.vlm
        screen = (
            VisionScreenAdapter(frame_client, vlm, timeout_ms=local_cfg.vision_timeout_ms)
            if frame_client is not None and vlm is not None
            else None
        )
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/cognition/test_pipeline.py tests/cognition/test_assembly.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/pipeline.py src/yuki/cognition/assembly.py tests/cognition/test_pipeline.py
git commit -m "refactor: expose frame_client/vlm as public properties, drop private reflection"
```

---

### Task 4: 拆出 AsrSession

**Files:**
- Create: `src/yuki/cognition/asr_session.py`
- Create: `tests/cognition/test_asr_session.py`
- Modify: `src/yuki/cognition/pipeline.py`
- Modify: `tests/cognition/test_pipeline.py`

**Interfaces:**
- Consumes: `SpeechBuffer`（`src/yuki/cognition/speech_buffer.py`）。
- Produces: `AsrSession(*, listen_timeout_s, listen_window_s, pre_roll_s, audio_frame_ms, clock, speech_buffer=None, on_utterance=None)`，方法 `begin() -> list`（on_awake，返回 pre-roll）、`feed(samples) -> bool`（on_mic，返回是否 listening）、`add_frame(samples)`、`has_speech() -> bool`、`consume_utterance(session_id) -> bool`、`is_current(session_id) -> bool`、`finish(session_id)`、`check_due() -> bool`、`return_to_idle()`、`reset()`；属性 `state`、`session_id`、`speech_buffer`。pipeline 委托。

- [ ] **Step 1: 创建 `tests/cognition/test_asr_session.py`（先红）**

```python
import numpy as np

from yuki.cognition.asr_session import AsrSession
from yuki.cognition.speech_buffer import SpeechBuffer


class FakeSpeechBuffer:
    def __init__(self):
        self.frames = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1
        self.frames = []

    def add_frame(self, samples):
        self.frames.append(samples)


def _session(now, sb=None, **kw):
    return AsrSession(
        listen_timeout_s=1.0,
        listen_window_s=0.5,
        pre_roll_s=0.04,
        audio_frame_ms=20,
        clock=lambda: now[0],
        speech_buffer=sb or FakeSpeechBuffer(),
        **kw,
    )


def test_begin_starts_listening_and_returns_pre_roll():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    assert session.begin() == []
    assert session.state == "listening"
    assert session.session_id is not None


def test_begin_ignored_when_not_idle():
    now = [10.0]
    session = _session(now)
    first = session.begin()
    second = session.begin()  # 已在 listening
    assert second == []
    assert session.state == "listening"


def test_feed_before_begin_returns_false():
    now = [10.0]
    session = _session(now)
    assert session.feed(np.zeros(320, dtype=np.float32)) is False
    assert session.state == "idle"


def test_feed_after_begin_buffers_and_returns_true():
    now = [10.0]
    sb = FakeSpeechBuffer()
    session = _session(now, sb)
    session.begin()
    assert session.feed(np.zeros(320, dtype=np.float32)) is True
    assert len(sb.frames) == 1


def test_check_due_returns_to_idle_after_timeout():
    now = [10.0]
    session = _session(now)
    session.begin()
    now[0] += 1.1
    assert session.check_due() is True
    assert session.state == "idle"


def test_consume_utterance_marks_processing():
    now = [10.0]
    session = _session(now)
    session.begin()
    sid = session.session_id
    assert session.consume_utterance(sid) is True
    assert session.state == "processing"
    # 过期 session 拒绝
    assert session.consume_utterance(sid + 999) is False


def test_is_current_matches_session():
    now = [10.0]
    session = _session(now)
    session.begin()
    sid = session.session_id
    assert session.is_current(sid) is True
    assert session.is_current(sid + 1) is False
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_asr_session.py -v`
Expected: FAIL（`ModuleNotFoundError: yuki.cognition.asr_session`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/asr_session.py`**

```python
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from yuki.cognition.speech_buffer import SpeechBuffer


class AsrSession:
    """ASR 状态机：idle→listening→speaking/processing→idle，含 pre-roll 与超时回退。

    从 PerceptionPipeline 拆出；不依赖 bus/topic，纯逻辑可测。
    """

    def __init__(
        self,
        *,
        listen_timeout_s: float = 10.0,
        listen_window_s: float = 5.0,
        pre_roll_s: float = 1.2,
        audio_frame_ms: int = 20,
        clock: Callable[[], float] = time.monotonic,
        speech_buffer: SpeechBuffer | None = None,
        on_utterance=None,
    ) -> None:
        self._listen_timeout_s = max(0.0, float(listen_timeout_s))
        self._listen_window_s = max(0.0, float(listen_window_s))
        self._clock = clock
        self._speech_buffer = speech_buffer or SpeechBuffer(on_utterance=on_utterance)
        self._pre_roll: deque = deque(
            maxlen=int(max(0.0, float(pre_roll_s)) * 1000 / max(1, int(audio_frame_ms)))
        )
        self._state = "idle"
        self._listening = False
        self._generation = 0
        self._session_id: int | None = None
        self._last_activity = self._clock()
        self._current_timeout_s = self._listen_timeout_s

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def speech_buffer(self) -> SpeechBuffer:
        return self._speech_buffer

    def begin(self) -> list:
        """on_awake：idle→listening，返回需回灌的 pre-roll 帧。"""
        if self._state != "idle":
            return []
        self._generation += 1
        self._session_id = self._generation
        self._state = "listening"
        self._listening = True
        self._last_activity = self._clock()
        self._current_timeout_s = self._listen_timeout_s
        pre_roll = list(self._pre_roll)
        self._speech_buffer.reset()
        return pre_roll

    def feed(self, samples: np.ndarray) -> bool:
        """on_mic：累积 pre-roll；listening 时返回 True 供调用方 add_frame。"""
        self._pre_roll.append(samples)
        return self._listening

    def add_frame(self, samples: np.ndarray) -> None:
        self._speech_buffer.add_frame(samples)
        if not self._listening:
            return
        if self.has_speech():
            self._state = "speaking"
            self._last_activity = self._clock()

    def has_speech(self) -> bool:
        speech = getattr(self._speech_buffer, "_speech", None)
        return bool(speech)

    def consume_utterance(self, session_id: int | None) -> bool:
        if not self._listening or self._session_id != session_id:
            return False
        self._state = "processing"
        self._last_activity = self._clock()
        return True

    def is_current(self, session_id: int | None) -> bool:
        return session_id is not None and self._listening and self._session_id == session_id

    def finish(self, session_id: int | None) -> None:
        if session_id is not None and self._session_id != session_id:
            return
        if not self._listening:
            return
        if self.has_speech():
            self._state = "speaking"
            return
        self._state = "listening"
        self._last_activity = self._clock()
        self._current_timeout_s = self._listen_window_s

    def check_due(self) -> bool:
        if self._state != "listening":
            return False
        if self._current_timeout_s <= 0:
            return False
        if self._clock() - self._last_activity < self._current_timeout_s:
            return False
        self.return_to_idle()
        return True

    def return_to_idle(self) -> None:
        self._generation += 1
        self._session_id = None
        self._state = "idle"
        self._listening = False
        self._current_timeout_s = self._listen_timeout_s
        self._speech_buffer.reset()

    def reset(self) -> None:
        self._pre_roll.clear()
        self._speech_buffer.reset()
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_asr_session.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 改写 `src/yuki/cognition/pipeline.py` 委托 AsrSession**

- import 区新增：`from yuki.cognition.asr_session import AsrSession`
- `__init__` 中把 ASR 状态字段替换为单个 `self._asr`：

```python
        self._asr = AsrSession(
            listen_timeout_s=listen_timeout_s,
            listen_window_s=listen_window_s,
            pre_roll_s=pre_roll_s,
            audio_frame_ms=audio_frame_ms,
            clock=clock,
            speech_buffer=speech_buffer,
            on_utterance=self._on_utterance,
        )
        self._asr_lock = threading.RLock()  # 兼容旧测试访问
```

- `on_awake`：

```python
    def on_awake(self, topic: str, payload: dict) -> None:
        pre_roll = self._asr.begin()
        for samples in pre_roll:
            self._asr.add_frame(samples)
```

- `on_mic`：

```python
    def on_mic(self, topic: str, payload: dict) -> None:
        pcm_b64 = payload.get("pcm", "")
        if not pcm_b64:
            return
        import numpy as np
        try:
            raw = base64.b64decode(pcm_b64)
        except (TypeError, ValueError, binascii.Error):
            logger.warning("mic frame decode failed")
            return
        samples = np.frombuffer(raw, dtype=np.float32)
        if self._asr.feed(samples):
            self._asr.add_frame(samples)
```

- `_on_utterance`：

```python
    def _on_utterance(self, samples) -> None:
        sid = self._asr.session_id
        if not self._asr.consume_utterance(sid):
            return
        self._stt_worker.submit(self._recognize_utterance, samples, sid)
```

- `_recognize_utterance` 内 `_session_is_current` → `self._asr.is_current(session_id)`，`_finish_stt_session` → `self._asr.finish(session_id)`：

```python
    def _recognize_utterance(self, samples, session_id: int | None = None) -> None:
        text = ""
        try:
            text = self._stt.recognize(samples, sample_rate=self._audio_sample_rate)
        except Exception:
            logger.exception("stt recognition failed")
        finally:
            should_publish = bool(text) and self._asr.is_current(session_id)
            try:
                if should_publish:
                    self._bus.publish(Topics.USER_UTTERANCE, {
                        "text": text,
                        "duration_s": round(len(samples) / self._audio_sample_rate, 2),
                        "ts": time.time(),
                    })
            finally:
                self._asr.finish(session_id)
```

- `check_asr_due`：

```python
    def check_asr_due(self) -> bool:
        return self._asr.check_due()
```

- **删除** `_add_frame_to_speech_buffer`、`_speech_buffer_has_speech`、`_session_is_current`、`_finish_stt_session`、`_return_to_idle_locked` 及 `_asr_state/_listening/_asr_generation/_session_id/_last_activity_monotonic/_current_listen_timeout_s/_pre_roll` 字段。保留 `_speech_buffer` 只读别名供旧测试：

```python
    @property
    def _speech_buffer(self):
        return self._asr.speech_buffer
```

- [ ] **Step 6: 更新 `tests/cognition/test_pipeline.py` 的私有断言**

- `test_pipeline_awake_timeout_returns_to_idle`：`pipeline._asr_state == "idle"` → `pipeline._asr.state == "idle"`。
- `test_pipeline_listen_window_timeout_after_empty_stt`：`pipeline._session_id` → `pipeline._asr.session_id`；`pipeline._asr_state` → `pipeline._asr.state`。
- `test_pipeline_discards_stale_stt_result`：`stale_session = pipeline._session_id` → `pipeline._asr.session_id`；`with pipeline._asr_lock: pipeline._return_to_idle_locked()` → `pipeline._asr.return_to_idle()`。

- [ ] **Step 7: 运行验证通过**

Run: `python -m pytest tests/cognition/test_pipeline.py tests/cognition/test_asr_session.py tests/cognition/test_speech_buffer.py -v`
Expected: 全 PASS。

- [ ] **Step 8: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 9: Commit**

```bash
git add src/yuki/cognition/asr_session.py src/yuki/cognition/pipeline.py tests/cognition/test_asr_session.py tests/cognition/test_pipeline.py
git commit -m "refactor: extract ASR state machine into standalone AsrSession"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 9 全目标——DecisionSink 解耦（Task 1）、ScreenQueryPort 依赖反转（Task 2）、公开属性消除反射（Task 3）、AsrSession 拆出（Task 4）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `DecisionSink.on_user_utterance(text)` 在 Task 1 定义，`_handle_locked` 与 `test_hub.py::FakeSedimenter` 同步改为单参；`ScreenQueryPort` 三方法在 Task 2 定义，`perception_tools` 与测试同签名；`AsrSession` 方法名在 Task 4 Step 1 测试与 Step 3 实现一一对应。
- **行为等价：** 每个任务均为纯重构——Task 1 保留 trusted_metadata 门与 crisis 门；Task 4 保留 `_asr_lock` 别名与 `_speech_buffer` 属性避免破坏既有测试；pipeline 的 topic 发布行为不变。
