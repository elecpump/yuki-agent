# Yuki 反馈闭环环2(偏好沉淀) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现环2 偏好沉淀——`PreferenceSedimenter` 把重复反馈模式沉淀为带置信度的偏好记忆（可显式纠正），并回馈 tuner 冷却偏置。

**Architecture:** 新增 `src/yuki/cognition/brain/sedimenter.py`（维度计数/置信度/沉淀/显式纠正），与 `FeedbackTuner` 解耦、共用 `detect_polarity`（从 tuner 抽出共享函数）。hub 在同一信号点喂入（`on_user_utterance`/`on_engagement`）；agent 装配。沉淀结果 = MemoryManager preference 记忆（L2 已消费）。

**Tech Stack:** Python ≥3.11，stdlib + 现有 pydantic/structlog。零新增运行时依赖。

## Global Constraints

- 零新增运行时依赖；零协议变更（REPLY 主题/载荷不变）。
- `detect_polarity(text) -> "negative"|"positive"|"neutral"`（tuner.py 抽出共享；负/正关键词同现有 NEGATIVE_KEYWORDS/POSITIVE_KEYWORDS）。
- `FeedbackTuner.set_cooldown_floor(value)`：`_min_s = max(_min_s, value)`；若当前 `_cooldown < _min_s` → 提升到 `_min_s` 并同步 policy + soul。`_min_s` 为冷却钳制下限。
- `PreferenceSedimenter(memory, *, tuner=None, min_signals=3, confidence_threshold=0.6, topic_engagement_threshold=3, frequency_floor_s=120.0)`：
  - `on_user_utterance(text, intent)`：intent==SYSTEM 时先检纠正词（写显式偏好 + 删全部 source="feedback" 隐式偏好，§8.3 简化实现），再检陈述词（写显式偏好 source="user" confidence=1.0）；否则按 `detect_polarity` 强化节奏维度。
  - `on_engagement(topic)`：同 topic 计数 ≥ `topic_engagement_threshold` → 写 "对{topic}话题感兴趣"（label `yuki.topic.{topic}`, source="feedback"）。
  - 计数模型：每 label 维护 `{hits, contradicts}`；置信度 = `hits/(hits+1)`；`hits >= min_signals` 且 `hits/(hits+contradicts) >= confidence_threshold` → 沉淀（写/更新：按 metadata.label 找旧行删+写新）；低于阈值已沉淀 → 删除（降级）。
  - 节奏 label：`yuki.rhythm.frequency.low`（负→"用户不喜欢频繁主动开口"）、`yuki.rhythm.frequency.high`（正→"用户喜欢主动互动"）、`yuki.rhythm.length.short`（负且含 话多/啰嗦/太长/简单点 → "用户希望回复更简短"）。反向强化时对对立 label contradicts+1。
  - 频率偏好沉淀且 confidence ≥ threshold 时 `tuner.set_cooldown_floor(frequency_floor_s)`。
- `DecisionHub` 增 `sedimenter=None`：UTTERANCE 时（分类后）`sedimenter.on_user_utterance(text, intent)`；UTTERANCE 且 `effective_situation.topic` 非空 → `sedimenter.on_engagement(topic)`。`build_brain(..., sedimenter=None)`。
- `CognitionAgent.setup` 装配 `PreferenceSedimenter(memory, tuner=tuner, min_signals=config.sedimenter.min_signals, confidence_threshold=config.sedimenter.confidence_threshold, topic_engagement_threshold=config.sedimenter.topic_engagement_threshold)` 传给 build_brain。
- `Config` 增 `sedimenter:` 节（min_signals=3/confidence_threshold=0.6/topic_engagement_threshold=3，env `YUKI_SEDIMENTER_*`）。
- e2e 等价：沉淀阈值需 ≥3 次信号，单测才触发；sedimenter=None 时行为不变。
- 测试命令（仓库根）：`& ".venv\Scripts\python.exe" -m pytest <文件> -v`；全仓 `-m pytest`。
- 设计文档：`docs/superpowers/specs/2026-08-14-feedback-ring2-sedimenter-design.md`（已提交）。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/sedimenter.py`
- `tests/cognition/test_sedimenter.py`

**修改**
- `src/yuki/cognition/brain/tuner.py`（detect_polarity 抽出 + set_cooldown_floor）、`tests/cognition/test_tuner.py`
- `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`（sedimenter 节）
- `src/yuki/cognition/brain/hub.py`（sedimenter 喂入）、`tests/cognition/test_hub.py`
- `src/yuki/cognition/agent.py`（装配）、`tests/cognition/test_cognition.py`

---

### Task 1: sedimenter 配置 + tuner 重构（detect_polarity + set_cooldown_floor）

**Files:**
- Modify: `src/yuki/cognition/brain/tuner.py`、`tests/cognition/test_tuner.py`
- Modify: `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`

**Interfaces:**
- Consumes: `FeedbackTuner`（现有）。
- Produces: `detect_polarity(text) -> str`；`FeedbackTuner.set_cooldown_floor(value)`；`Config.sedimenter`（SedimenterConfig）。Task 2 依赖。

- [ ] **Step 1: 追加 tuner 测试到 `tests/cognition/test_tuner.py`**

```python
def test_detect_polarity():
    from yuki.cognition.brain.tuner import detect_polarity
    assert detect_polarity("太吵了") == "negative"
    assert detect_polarity("说得好") == "positive"
    assert detect_polarity("随便聊聊") == "neutral"
    assert detect_polarity("") == "neutral"


def test_set_cooldown_floor_raises_min(tmp_path):
    policy = DecisionPolicy(120.0)
    soul = SoulStore(tmp_path / "s.json", "yuki")
    tuner = FeedbackTuner(policy, soul, cooldown_min_s=30.0)
    tuner.set_cooldown_floor(200.0)
    assert tuner.cooldown_s == 200.0   # 当前 120 < 200 → 提升
    assert policy.cooldown_s == 200.0
    assert soul.load()["proactive_cooldown_s"] == pytest.approx(200.0)
    # 后续 adjust 不再低于 floor
    tuner.adjust(0.5)
    assert tuner.cooldown_s >= 200.0


def test_set_cooldown_floor_lower_than_min_noop(tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = FeedbackTuner(policy, SoulStore(tmp_path / "s.json", "yuki"), cooldown_min_s=30.0)
    tuner.set_cooldown_floor(20.0)  # 低于现有 min → no-op
    assert tuner._min_s == 30.0
    assert tuner.cooldown_s == 120.0
```

- [ ] **Step 2: 追加 sedimenter 配置测试到 `tests/test_config.py`**

```python
def test_sedimenter_defaults():
    config = Config()
    assert config.sedimenter.min_signals == 3
    assert config.sedimenter.confidence_threshold == 0.6
    assert config.sedimenter.topic_engagement_threshold == 3


def test_sedimenter_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_SEDIMENTER_MIN_SIGNALS", "5")
    monkeypatch.setenv("YUKI_SEDIMENTER_CONFIDENCE_THRESHOLD", "0.8")
    config = Config.load(None)
    assert config.sedimenter.min_signals == 5
    assert config.sedimenter.confidence_threshold == 0.8
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py tests/test_config.py -v`
Expected: FAIL（`ImportError: cannot import name 'detect_polarity'` / `AttributeError: ... cooldown_min_s` 或 `sedimenter` 缺失）。

- [ ] **Step 4: `src/yuki/cognition/brain/tuner.py` 重构**

抽出 `detect_polarity`（在 `FeedbackTuner` 类外、常量后）：

```python
def detect_polarity(text: str) -> str:
    lowered = (text or "").lower()
    if any(kw in lowered for kw in NEGATIVE_KEYWORDS):
        return "negative"
    if any(kw in lowered for kw in POSITIVE_KEYWORDS):
        return "positive"
    return "neutral"
```

`FeedbackTuner.on_user_utterance` 中极性判定改为：

```python
        polarity = detect_polarity(text)
        if polarity == "negative":
            self.adjust(1.5)
            self._open_ts = None
            return
        if polarity == "positive":
            self.adjust(0.8)
            self._open_ts = None
            return
```

（原 `lowered`/关键词分支移除，行为不变。）

`FeedbackTuner` 新增：

```python
    def set_cooldown_floor(self, value: float) -> None:
        self._min_s = max(self._min_s, value)
        if self._cooldown < self._min_s:
            self._cooldown = self._min_s
            self._policy.set_cooldown_s(self._cooldown)
            self._soul.save({COOLDOWN_KEY: self._cooldown})
```

- [ ] **Step 5: `src/yuki/config.py` 加 SedimenterConfig 并注册**

在 `ContextConfig` 之后新增：

```python
class SedimenterConfig(BaseModel):
    min_signals: int = Field(3, ge=1)
    confidence_threshold: float = Field(0.6, ge=0.0, le=1.0)
    topic_engagement_threshold: int = Field(3, ge=1)
```

在 `Config` 中 `context` 字段之后新增：

```python
    sedimenter: SedimenterConfig = Field(default_factory=SedimenterConfig)
```

在 `Config.load` 的 section 元组中 `("context", ContextConfig),` 之后新增：

```python
            ("sedimenter", SedimenterConfig),
```

- [ ] **Step 6: `config.example.yaml` 加 sedimenter 节**

```yaml
sedimenter:
  min_signals: 3
  confidence_threshold: 0.6
  topic_engagement_threshold: 3
```

- [ ] **Step 7: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py tests/test_config.py -v`
Expected: 全 PASS（既有 tuner 测试保持通过，证明 detect_polarity 抽取无行为变化）。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/cognition/brain/tuner.py src/yuki/config.py config.example.yaml tests/cognition/test_tuner.py tests/test_config.py
git commit -m "feat: extract detect_polarity, add cooldown floor, add sedimenter config"
```

---

### Task 2: PreferenceSedimenter

**Files:**
- Create: `src/yuki/cognition/brain/sedimenter.py`
- Test: `tests/cognition/test_sedimenter.py`

**Interfaces:**
- Consumes: `detect_polarity`（Task 1）、`MemoryManager`、`FeedbackTuner`（可选）。
- Produces: `PreferenceSedimenter`（§Global Constraints 签名与语义）。Task 3 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_sedimenter.py`**

```python
import pytest

from yuki.cognition.brain.sedimenter import (
    LABEL_FREQUENCY_HIGH,
    LABEL_FREQUENCY_LOW,
    PreferenceSedimenter,
)
from yuki.cognition.brain.tuner import FeedbackTuner
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.classifier import Intent
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


def make_sed(tmp_path, **kwargs):
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    return PreferenceSedimenter(manager, **kwargs), manager


def labels(memory):
    return [m["metadata"].get("label") for m in memory.list(memory_type="preference")]


def test_rhythm_frequency_sediments_after_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW in labels(memory)


def test_rhythm_not_sedimented_below_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert labels(memory) == []  # 仅 2 次，未达 3


def test_contradicting_signals_lower_confidence(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW in labels(memory)
    # 4 次正向反向 → low 的 contradicts=4 → 置信度 3/(3+4)=0.43 < 0.6 → 降级删除
    for _ in range(4):
        sed.on_user_utterance("说得好", Intent.CHIT_CHAT)
    assert LABEL_FREQUENCY_LOW not in labels(memory)


def test_length_preference_when_verbose(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=3)
    for _ in range(3):
        sed.on_user_utterance("你话太多了，简短点", Intent.CHIT_CHAT)
    assert "yuki.rhythm.length.short" in labels(memory)


def test_explicit_statement_sediments(tmp_path):
    sed, memory = make_sed(tmp_path)
    sed.on_user_utterance("别讲笑话了", Intent.SYSTEM)
    prefs = memory.list(memory_type="preference")
    assert prefs and prefs[0]["source"] == "user"
    assert prefs[0]["confidence"] == 1.0


def test_correction_wipes_implicit_and_pins_explicit(tmp_path):
    sed, memory = make_sed(tmp_path, min_signals=1)
    sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)  # 隐式（feedback source）
    assert any(m["source"] == "feedback" for m in memory.list(memory_type="preference"))
    sed.on_user_utterance("其实我不喜欢主动聊天", Intent.SYSTEM)  # 纠正
    prefs = memory.list(memory_type="preference")
    assert not any(m["source"] == "feedback" for m in prefs)
    assert prefs and prefs[0]["source"] == "user"


def test_topic_interest_sediments_after_threshold(tmp_path):
    sed, memory = make_sed(tmp_path, topic_engagement_threshold=3)
    for _ in range(3):
        sed.on_engagement("量子计算")
    assert "yuki.topic.量子计算" in labels(memory)


def test_frequency_preference_sets_tuner_floor(tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = FeedbackTuner(policy, SoulStore(tmp_path / "s.json", "yuki"), cooldown_min_s=30.0)
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    sed = PreferenceSedimenter(memory, tuner=tuner, min_signals=3, confidence_threshold=0.6)
    for _ in range(3):
        sed.on_user_utterance("太吵了", Intent.CHIT_CHAT)
    assert tuner._min_s >= 120.0
    assert tuner.cooldown_s >= 120.0
```

（注意：`test_contradicting_signals_lower_confidence` 用 4 次反向使低频置信度 3/7≈0.43 < 0.6 触发降级删除。`make_sed` 的 tmp_path 由各测试 fixture 提供。）

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_sedimenter.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.sedimenter'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/sedimenter.py`**

```python
from yuki.cognition.brain.classifier import Intent
from yuki.cognition.brain.tuner import FeedbackTuner, detect_polarity
from yuki.memory.manager import MemoryManager

LABEL_FREQUENCY_LOW = "yuki.rhythm.frequency.low"
LABEL_FREQUENCY_HIGH = "yuki.rhythm.frequency.high"
LABEL_LENGTH_SHORT = "yuki.rhythm.length.short"

RHYTHM_CONTENTS = {
    LABEL_FREQUENCY_LOW: "用户不喜欢频繁主动开口",
    LABEL_FREQUENCY_HIGH: "用户喜欢主动互动",
    LABEL_LENGTH_SHORT: "用户希望回复更简短",
}

CORRECTION_KEYWORDS = ("其实我不", "说反了", "我改主意了", "我说错了")
STATEMENT_KEYWORDS = ("我喜欢", "我不喜欢", "我希望", "请", "别", "不要", "讨厌")
LENGTH_KEYWORDS = ("话多", "啰嗦", "太长", "简单点")


class PreferenceSedimenter:
    """环2 偏好沉淀：重复反馈模式 → 偏好记忆（带置信度），可显式纠正。"""

    def __init__(self, memory: MemoryManager, *, tuner: FeedbackTuner | None = None,
                 min_signals: int = 3, confidence_threshold: float = 0.6,
                 topic_engagement_threshold: int = 3,
                 frequency_floor_s: float = 120.0) -> None:
        self._memory = memory
        self._tuner = tuner
        self._min_signals = min_signals
        self._confidence_threshold = confidence_threshold
        self._topic_threshold = topic_engagement_threshold
        self._frequency_floor_s = frequency_floor_s
        self._counts: dict[str, dict] = {}
        self._topics: dict[str, int] = {}
        self._sedimented: set[str] = set()

    def on_user_utterance(self, text: str, intent) -> None:
        text = text or ""
        if intent == Intent.SYSTEM:
            if any(kw in text for kw in CORRECTION_KEYWORDS):
                self._apply_correction(text)
                return
            if any(kw in text for kw in STATEMENT_KEYWORDS):
                self._write_explicit(text)
                return
        polarity = detect_polarity(text)
        if polarity == "negative":
            self._reinforce(LABEL_FREQUENCY_LOW, LABEL_FREQUENCY_HIGH)
            if any(kw in text for kw in LENGTH_KEYWORDS):
                self._reinforce(LABEL_LENGTH_SHORT, None)
        elif polarity == "positive":
            self._reinforce(LABEL_FREQUENCY_HIGH, LABEL_FREQUENCY_LOW)

    def on_engagement(self, topic: str) -> None:
        if not topic:
            return
        label = f"yuki.topic.{topic}"
        self._topics[label] = self._topics.get(label, 0) + 1
        if self._topics[label] == self._topic_threshold:
            self._write_preference(f"对{topic}话题感兴趣", label, source="feedback", confidence=0.8)

    def _reinforce(self, label: str, opposite: str | None) -> None:
        entry = self._counts.setdefault(label, {"hits": 0, "contradicts": 0})
        entry["hits"] += 1
        if opposite:
            opp = self._counts.setdefault(opposite, {"hits": 0, "contradicts": 0})
            opp["contradicts"] += 1
            self._maybe_sediment(opposite)  # 反向也重新评估
        self._maybe_sediment(label)

    def _maybe_sediment(self, label: str) -> None:
        entry = self._counts[label]
        total = entry["hits"] + entry["contradicts"]
        confidence = entry["hits"] / max(1, total)
        if entry["hits"] >= self._min_signals and confidence >= self._confidence_threshold:
            self._write_preference(RHYTHM_CONTENTS[label], label, source="feedback", confidence=confidence)
            self._sedimented.add(label)
            if label == LABEL_FREQUENCY_LOW and self._tuner is not None:
                self._tuner.set_cooldown_floor(self._frequency_floor_s)
        elif label in self._sedimented:
            # 已沉淀但被反向信号拉低 → 降级删除（计数器进程内，不误删旧会话沉淀）
            self._remove_by_label(label)
            self._sedimented.discard(label)

    def _write_explicit(self, text: str) -> None:
        self._write_preference(text, "yuki.explicit", source="user", confidence=1.0)

    def _apply_correction(self, text: str) -> None:
        # 简化实现（§8.3 显式>隐式）：删全部隐式偏好，写显式纠正偏好
        for m in self._memory.list(memory_type="preference"):
            if m.get("source") == "feedback":
                self._memory.delete(m["id"])
        self._write_explicit(text)

    def _write_preference(self, content: str, label: str, *, source: str, confidence: float) -> None:
        self._remove_by_label(label)
        self._memory.write("preference", content, confidence=confidence, source=source,
                           metadata={"label": label})

    def _remove_by_label(self, label: str) -> None:
        for m in self._memory.list(memory_type="preference"):
            if m.get("metadata", {}).get("label") == label:
                self._memory.delete(m["id"])
```

（注：`Intent` 从 `yuki.cognition.brain.classifier` 导入——brain 包的 classifier 模块；若实际路径不同以导入为准。）

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_sedimenter.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/sedimenter.py tests/cognition/test_sedimenter.py
git commit -m "feat: add PreferenceSedimenter with confidence-based preference sedimentation"
```

---

### Task 3: DecisionHub 接线 + Agent 装配 + 回归

**Files:**
- Modify: `src/yuki/cognition/brain/hub.py`、`tests/cognition/test_hub.py`
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `PreferenceSedimenter`（Task 2）、`build_brain`。
- Produces: `DecisionHub.__init__` 增 `sedimenter=None`；`build_brain(..., sedimenter=None)`；`CognitionAgent.setup` 装配。全仓回归。

- [ ] **Step 1: 追加 hub 测试到 `tests/cognition/test_hub.py`**

```python
class FakeSedimenter:
    def __init__(self):
        self.utterances = []
        self.topics = []

    def on_user_utterance(self, text, intent):
        self.utterances.append((text, intent))

    def on_engagement(self, topic):
        self.topics.append(topic)


def test_hub_feeds_sedimenter(hub):
    h, bus, _ = hub
    sed = FakeSedimenter()
    h._sedimenter = sed
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "太吵了", "duration_s": 1.0, "ts": 0.0})
    assert sed.utterances and sed.utterances[0][0] == "太吵了"


def test_hub_feeds_engagement_topic(hub):
    h, bus, _ = hub
    sed = FakeSedimenter()
    h._sedimenter = sed
    h._context = {"topic": "量子计算", "sensitive": False}
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert "量子计算" in sed.topics
```

- [ ] **Step 2: 追加 agent 测试到 `tests/cognition/test_cognition.py`**

```python
def test_cognition_agent_builds_sedimenter(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(context={"snapshot_path": str(tmp_path / "snap.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._sedimenter is not None
    finally:
        agent.teardown()
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py tests/cognition/test_cognition.py -v`
Expected: FAIL（`AttributeError: 'DecisionHub' object has no attribute '_sedimenter'`）。

- [ ] **Step 4: `src/yuki/cognition/brain/hub.py` 接线**

- `DecisionHub.__init__` 增 `sedimenter=None`，存 `self._sedimenter = sedimenter`。
- `_handle` 中（写侧块附近、trace 之前）追加：

```python
        if self._sedimenter is not None and trigger == TriggerKind.UTTERANCE:
            self._sedimenter.on_user_utterance(text, intent)
            topic = (effective_situation or {}).get("topic")
            if topic:
                self._sedimenter.on_engagement(topic)
```

- `build_brain(..., sedimenter=None)` 透传给 DecisionHub。

- [ ] **Step 5: `src/yuki/cognition/agent.py` 装配**

import 增补：

```python
from yuki.cognition.brain.sedimenter import PreferenceSedimenter
```

`setup()` 中，`tuner.load_soul()` 之后、`build_brain` 调用处，构建 sedimenter 并传入：

```python
        sedimenter = PreferenceSedimenter(
            self._memory,
            tuner=tuner,
            min_signals=self.config.sedimenter.min_signals,
            confidence_threshold=self.config.sedimenter.confidence_threshold,
            topic_engagement_threshold=self.config.sedimenter.topic_engagement_threshold,
        )
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
            context=context,
            projector=projector,
            sedimenter=sedimenter,
        )
```

（替换原 `build_brain(...)` 调用，新增 `sedimenter=`。）

- [ ] **Step 6: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py tests/cognition/test_cognition.py tests/cognition/test_sedimenter.py tests/cognition/test_tuner.py -v`
Expected: 全 PASS。

- [ ] **Step 7: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（此前 381 passed 基础上新增 sedimenter 相关测试）。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: wire PreferenceSedimenter into DecisionHub and CognitionAgent"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1/2/3；§3 维度/信号 → Task 2；§4 置信度模型 → Task 2；§5 显式纠正 → Task 2；§6 消费（tuner 偏置）→ Task 1/2；§7 配置 → Task 1；§8 测试 → 各任务；§9/§10 ADR → 贯穿。
- **一致性**：`detect_polarity` 在 Task 1 抽出、Task 2 sedimenter 消费；`set_cooldown_floor` 在 Task 1 定义、Task 2 频率沉淀时调用；`PreferenceSedimenter` 构造签名（min_signals/confidence_threshold/topic_engagement_threshold）在 Task 2 定义、Task 3 agent 用 config 装配；hub 的 sedimenter 喂入点与 §3 信号 API 一致。
- **兼容**：tuner 的 `on_user_utterance` 改用 `detect_polarity` 后行为不变（既有测试保护）；sedimenter=None 时 hub 行为不变；e2e 不变。
- **测试注意**：`test_contradicting_signals_lower_confidence` 的反向次数需使低频置信度低于阈值（实现时调整）；`classifier` 导入路径以实际为准。
