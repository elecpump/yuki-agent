# Yuki 反馈闭环环1(参数自调 + soul) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现环1 参数自调——`FeedbackTuner`(隐式回应 + 显式话语反馈实时调整主动开口冷却)+ `SoulStore`(版本化 json 持久化),接入 DecisionHub 与 CognitionAgent。

**Architecture:** 新增 `src/yuki/cognition/brain/soul.py`(`SoulStore`)与 `tuner.py`(`FeedbackTuner` + 极性关键词)。`DecisionPolicy` 增 `set_cooldown_s`/`cooldown_s` 属性;`DecisionHub` 增 `tuner`(SITUATION spoke → `on_proactive_open`,UTTERANCE → `on_user_utterance`);`build_brain`/agent 装配共享 policy + tuner。`soul:` 配置(默认 `data/soul.json`)。

**Tech Stack:** Python ≥3.11,stdlib json/time + 现有 pydantic。零新增运行时依赖。

## Global Constraints

- 零新增运行时依赖;零协议变更(REPLY 主题/载荷不变)。
- `SoulStore(path, persona_name, persona_version=1)`:`load() -> dict | None`(文件缺失/损坏/名字或版本不符 → None)、`save(params)`(写 `{persona_name, persona_version, params, updated_at}`,父目录自动创建,写失败仅告警)、`reset()`(删文件,幂等)。
- `FeedbackTuner(policy, soul, *, window_s=90.0, cooldown_min_s=30.0, cooldown_max_s=600.0)`:`load_soul()` 恢复冷却;`on_proactive_open()` 记录 `_open_ts`;`on_user_utterance(text)` 决策树(超时→1.3 降 / 负极性→1.5 强降 / 正极性→0.8 升 / 窗口内接话→0.9 升,极性与接话互斥);`adjust(factor)` 钳制 `[min,max]` 后 `policy.set_cooldown_s` + `soul.save`;`cooldown_s` 属性。
- 极性关键词:`NEGATIVE_KEYWORDS = ("太吵","吵","话多","话太多","安静","闭嘴","少说","啰嗦","别说了")`、`POSITIVE_KEYWORDS = ("说得好","好听","有意思","继续","再来","棒","可爱")`(子串匹配,文本小写化)。
- `DecisionPolicy` 增 `set_cooldown_s(value)` 与 `cooldown_s` 只读属性;`_decide_situation` 读当前可变值。
- `DecisionHub(bus, ..., tuner=None)`:`tuner` 非 None 时——SITUATION 且 spoke → `tuner.on_proactive_open()`;UTTERANCE → `tuner.on_user_utterance(text)`(决策后调用,不影响本次);`build_brain(..., tuner=None)` 透传。
- `CognitionAgent.setup`:`DecisionPolicy(...)` 由 agent 构建并与 `SoulStore(config.soul.path, config.persona_name)` 组装 `FeedbackTuner`,`tuner.load_soul()`,policy+tuner 传入 `build_brain`。
- `Config` 增 `soul:` 节(`path="data/soul.json"`,env `YUKI_SOUL_PATH`);`config.example.yaml` 加 soul 节。
- e2e 等价:awake → `我在,你说。`;无反馈时无 soul 文件、冷却回 config 默认。
- 测试命令(仓库根):`& ".venv\Scripts\python.exe" -m pytest <文件> -v`;全仓 `-m pytest`。
- 设计文档:`docs/superpowers/specs/2026-08-14-feedback-tuning-soul-design.md`(已提交)。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/soul.py`、`src/yuki/cognition/brain/tuner.py`
- `tests/cognition/test_soul.py`、`tests/cognition/test_tuner.py`

**修改**
- `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`(soul 节)
- `src/yuki/cognition/brain/policy.py`(set_cooldown_s/cooldown_s)、`tests/cognition/test_policy.py`
- `src/yuki/cognition/brain/hub.py`(tuner 接线)、`tests/cognition/test_hub.py`
- `src/yuki/cognition/agent.py`(policy+soul+tuner 装配)、`tests/cognition/test_cognition.py`

---

### Task 1: SoulStore + soul 配置

**Files:**
- Create: `src/yuki/cognition/brain/soul.py`
- Modify: `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- Test: `tests/cognition/test_soul.py`

**Interfaces:**
- Consumes: 无。
- Produces: `SoulStore(path, persona_name, persona_version=1)` 方法 `load() -> dict | None`、`save(params: dict)`、`reset()`。`Config.soul`(`SoulConfig`: path="data/soul.json")。Task 3 依赖。

- [ ] **Step 1: 追加 soul 配置测试到 `tests/test_config.py`**

```python
def test_soul_defaults():
    config = Config()
    assert config.soul.path == "data/soul.json"


def test_soul_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_SOUL_PATH", "tmp/soul.json")
    config = Config.load(None)
    assert config.soul.path == "tmp/soul.json"
```

- [ ] **Step 2: 写失败测试 `tests/cognition/test_soul.py`**

```python
from yuki.cognition.brain.soul import SoulStore


def test_save_then_load_roundtrip(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki", persona_version=1)
    store.save({"proactive_cooldown_s": 137.5})
    loaded = SoulStore(tmp_path / "soul.json", "yuki", 1).load()
    assert loaded == {"proactive_cooldown_s": 137.5}


def test_load_missing_returns_none(tmp_path):
    assert SoulStore(tmp_path / "nope.json", "yuki").load() is None


def test_load_wrong_persona_name_returns_none(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    store.save({"proactive_cooldown_s": 100.0})
    assert SoulStore(tmp_path / "s.json", "aki").load() is None


def test_load_wrong_version_returns_none(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki", persona_version=1)
    store.save({"proactive_cooldown_s": 100.0})
    assert SoulStore(tmp_path / "s.json", "yuki", persona_version=2).load() is None


def test_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SoulStore(path, "yuki").load() is None


def test_save_creates_parent_dirs(tmp_path):
    store = SoulStore(tmp_path / "nested" / "dir" / "soul.json", "yuki")
    store.save({"a": 1})
    assert (tmp_path / "nested" / "dir" / "soul.json").exists()


def test_reset_removes_file(tmp_path):
    store = SoulStore(tmp_path / "s.json", "yuki")
    store.save({"proactive_cooldown_s": 1.0})
    assert store._path.exists()
    store.reset()
    assert not store._path.exists()
    store.reset()  # 幂等，不抛
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_soul.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.soul'`）。

- [ ] **Step 4: 创建 `src/yuki/cognition/brain/soul.py`**

```python
import json
import time
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.soul")


class SoulStore:
    """soul 状态：版本化 json 参数记录（环1 调参落点，环3 人格快照前身）。

    只存参数（proactive_cooldown_s 等），不存 persona 提示词。
    文件缺失/损坏/名字或版本不符 → load 返回 None，调用方回默认。
    """

    def __init__(self, path: str | Path, persona_name: str, persona_version: int = 1) -> None:
        self._path = Path(path)
        self._persona_name = persona_name
        self._persona_version = persona_version

    def load(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("soul read failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        if data.get("persona_name") != self._persona_name or data.get("persona_version") != self._persona_version:
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            return None
        return params

    def save(self, params: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "persona_name": self._persona_name,
            "persona_version": self._persona_version,
            "params": params,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("soul write failed", error=str(exc))

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("soul reset failed", error=str(exc))
```

- [ ] **Step 5: `src/yuki/config.py` 加 SoulConfig 并注册**

在 `CloudConfig` 之后新增：

```python
class SoulConfig(BaseModel):
    path: str = "data/soul.json"
```

在 `Config` 中 `cloud` 字段之后新增：

```python
    soul: SoulConfig = Field(default_factory=SoulConfig)
```

在 `Config.load` 的 section 元组中 `("cloud", CloudConfig),` 之后新增：

```python
            ("soul", SoulConfig),
```

- [ ] **Step 6: `config.example.yaml` 加 soul 节**

```yaml
soul:
  path: data/soul.json
```

- [ ] **Step 7: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_soul.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/cognition/brain/soul.py src/yuki/config.py config.example.yaml tests/cognition/test_soul.py tests/test_config.py
git commit -m "feat: add versioned soul state store and soul config"
```

---

### Task 2: DecisionPolicy 可变冷却

**Files:**
- Modify: `src/yuki/cognition/brain/policy.py`、`tests/cognition/test_policy.py`

**Interfaces:**
- Consumes: `DecisionPolicy`（现有）。
- Produces: `DecisionPolicy.set_cooldown_s(value: float)`、`DecisionPolicy.cooldown_s` 只读属性。Task 3/4 依赖。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_policy.py`**

```python
def test_set_cooldown_s_changes_gate():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert policy.cooldown_s == 120.0
    policy.set_cooldown_s(200.0)
    assert policy.cooldown_s == 200.0
    # 原冷却 120 在 now-last=150 时会开口；新冷却 200 应静默
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "x", "sensitive": False},
                            last_open_ts=0.0, now=150.0)
    assert [a.name for a in actions] == ["stay_silent"]
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py -v`
Expected: FAIL（`AttributeError: 'DecisionPolicy' object has no attribute 'cooldown_s'`）。

- [ ] **Step 3: `src/yuki/cognition/brain/policy.py` 追加**

在 `DecisionPolicy.__init__` 之后、`tier_for` 之前追加：

```python
    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def set_cooldown_s(self, value: float) -> None:
        self._cooldown = value
```

（`_decide_situation` 已用 `self._cooldown`，无需改。）

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/policy.py tests/cognition/test_policy.py
git commit -m "feat: make proactive cooldown mutable on DecisionPolicy"
```

---

### Task 3: FeedbackTuner

**Files:**
- Create: `src/yuki/cognition/brain/tuner.py`
- Test: `tests/cognition/test_tuner.py`

**Interfaces:**
- Consumes: `DecisionPolicy`（Task 2）、`SoulStore`（Task 1）。
- Produces: `NEGATIVE_KEYWORDS`/`POSITIVE_KEYWORDS`/`COOLDOWN_KEY`；`FeedbackTuner(policy, soul, *, window_s=90.0, cooldown_min_s=30.0, cooldown_max_s=600.0)` 方法 `load_soul()`/`on_proactive_open()`/`on_user_utterance(text)`/`adjust(factor)`、属性 `cooldown_s`。Task 4 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_tuner.py`**

```python
import pytest

from yuki.cognition.brain import tuner as tuner_mod
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.tuner import FeedbackTuner


def make(policy=None, soul=None, tmp_path=None, **kwargs):
    policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
    soul = soul or SoulStore(tmp_path / "s.json", "yuki")
    return FeedbackTuner(policy, soul, **kwargs)


def test_initial_cooldown_from_policy(tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(tmp_path=tmp_path)
    assert tuner.cooldown_s == 120.0


def test_window_engagement_warms_up(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 30.0
    tuner.on_user_utterance("嗯嗯")
    assert tuner.cooldown_s == pytest.approx(120.0 * 0.9)
    assert policy.cooldown_s == pytest.approx(120.0 * 0.9)


def test_timeout_after_window_cools_down(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 200.0
    tuner.on_user_utterance("嗯")
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.3)


def test_explicit_negative_strong_cool(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("太吵了，安静点")
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_explicit_negative_applies_outside_window(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("你话太多了")  # 无 _open_ts 也生效
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_explicit_positive_warms(tmp_path):
    tuner = make(tmp_path=tmp_path)
    tuner.on_user_utterance("说得好，继续")
    assert tuner.cooldown_s == pytest.approx(120.0 * 0.8)


def test_negative_overrides_window_engagement(monkeypatch, tmp_path):
    policy = DecisionPolicy(120.0)
    tuner = make(policy=policy, tmp_path=tmp_path, window_s=90.0)
    t = [1000.0]
    monkeypatch.setattr(tuner_mod.time, "time", lambda: t[0])
    tuner.on_proactive_open()
    t[0] += 30.0
    tuner.on_user_utterance("太吵了")  # 窗口内但负极性 → 1.5 而非 0.9
    assert tuner.cooldown_s == pytest.approx(120.0 * 1.5)


def test_clamp_lower_and_upper(tmp_path):
    tuner = make(tmp_path=tmp_path, cooldown_min_s=30.0, cooldown_max_s=600.0)
    tuner.adjust(0.0001)
    assert tuner.cooldown_s == 30.0
    tuner.adjust(100000.0)
    assert tuner.cooldown_s == 600.0


def test_adjust_syncs_policy_and_soul(tmp_path):
    policy = DecisionPolicy(120.0)
    soul = SoulStore(tmp_path / "s.json", "yuki")
    tuner = FeedbackTuner(policy, soul)
    tuner.adjust(1.5)
    assert policy.cooldown_s == pytest.approx(180.0)
    assert soul.load()["proactive_cooldown_s"] == pytest.approx(180.0)


def test_load_soul_restores_cooldown(tmp_path):
    policy = DecisionPolicy(120.0)
    soul = SoulStore(tmp_path / "s.json", "yuki")
    soul.save({"proactive_cooldown_s": 240.0})
    tuner = FeedbackTuner(policy, soul)
    tuner.load_soul()
    assert tuner.cooldown_s == 240.0
    assert policy.cooldown_s == 240.0
```

（注意：`make` 的 `tmp_path` 一律由各测试的 `tmp_path` fixture 提供，不得传 `None`。）

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.tuner'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/tuner.py`**

```python
import time

from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.tuner")

NEGATIVE_KEYWORDS = ("太吵", "吵", "话多", "话太多", "安静", "闭嘴", "少说", "啰嗦", "别说了")
POSITIVE_KEYWORDS = ("说得好", "好听", "有意思", "继续", "再来", "棒", "可爱")

COOLDOWN_KEY = "proactive_cooldown_s"


class FeedbackTuner:
    """环1 参数自调：隐式回应 + 显式话语 → 调整主动开口冷却，持久化到 soul。"""

    def __init__(self, policy: DecisionPolicy, soul: SoulStore, *,
                 window_s: float = 90.0, cooldown_min_s: float = 30.0,
                 cooldown_max_s: float = 600.0) -> None:
        self._policy = policy
        self._soul = soul
        self._window_s = window_s
        self._min_s = cooldown_min_s
        self._max_s = cooldown_max_s
        self._open_ts = None
        self._cooldown = policy.cooldown_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def load_soul(self) -> None:
        params = self._soul.load()
        if params and isinstance(params.get(COOLDOWN_KEY), (int, float)):
            self._cooldown = float(params[COOLDOWN_KEY])
            self._policy.set_cooldown_s(self._cooldown)

    def on_proactive_open(self) -> None:
        self._open_ts = time.time()

    def on_user_utterance(self, text: str) -> None:
        self._check_timeout()
        lowered = (text or "").lower()
        if any(kw in lowered for kw in NEGATIVE_KEYWORDS):
            self.adjust(1.5)
            self._open_ts = None
            return
        if any(kw in lowered for kw in POSITIVE_KEYWORDS):
            self.adjust(0.8)
            self._open_ts = None
            return
        if self._open_ts is not None and time.time() - self._open_ts <= self._window_s:
            self.adjust(0.9)
            self._open_ts = None

    def _check_timeout(self) -> None:
        if self._open_ts is not None and time.time() - self._open_ts > self._window_s:
            self.adjust(1.3)
            self._open_ts = None

    def adjust(self, factor: float) -> None:
        new = min(max(self._cooldown * factor, self._min_s), self._max_s)
        if new == self._cooldown:
            return
        self._cooldown = new
        self._policy.set_cooldown_s(new)
        self._soul.save({COOLDOWN_KEY: new})
        logger.info("tuned cooldown", cooldown_s=new, factor=factor)
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_tuner.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/tuner.py tests/cognition/test_tuner.py
git commit -m "feat: add FeedbackTuner with implicit and explicit feedback tuning"
```

---

### Task 4: DecisionHub 接线 + CognitionAgent 装配

**Files:**
- Modify: `src/yuki/cognition/brain/hub.py`、`tests/cognition/test_hub.py`
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Test: `tests/cognition/test_hub.py`、`tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `FeedbackTuner`（Task 3）、`DecisionPolicy`（Task 2）、`SoulStore`（Task 1）、`build_brain`。
- Produces: `DecisionHub.__init__` 增 `tuner=None`;`build_brain(..., tuner=None)`;`CognitionAgent.setup` 构建 policy+soul+tuner 并传入。Task 之后（全仓回归）。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_hub.py`**

在文件末尾追加：

```python
class FakeTuner:
    def __init__(self):
        self.opens = 0
        self.utterances = []
        self.loaded = 0

    def load_soul(self):
        self.loaded += 1

    def on_proactive_open(self):
        self.opens += 1

    def on_user_utterance(self, text):
        self.utterances.append(text)


def test_hub_notifies_tuner_on_proactive_open(hub, monkeypatch):
    h, bus, _ = hub
    tuner = FakeTuner()
    h._tuner = tuner
    monkeypatch.setattr("time.time", lambda: 0.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert tuner.opens == 1


def test_hub_feeds_utterance_to_tuner(hub):
    h, bus, _ = hub
    tuner = FakeTuner()
    h._tuner = tuner
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert tuner.utterances == ["你好"]
```

- [ ] **Step 2: 追加失败测试到 `tests/cognition/test_cognition.py`**

```python
def test_cognition_agent_builds_tuner(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._tuner is not None
    finally:
        agent.teardown()
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py tests/cognition/test_cognition.py -v`
Expected: FAIL（`TypeError: DecisionHub.__init__() got an unexpected keyword argument 'tuner'` 或 `AttributeError: 'DecisionHub' object has no attribute '_tuner'`）。

- [ ] **Step 4: `src/yuki/cognition/brain/hub.py` 接线**

- `DecisionHub.__init__` 增 `tuner=None` 参数，存 `self._tuner = tuner`。
- `_handle` 在 spoke 发布块之后追加：

```python
        if self._tuner is not None:
            if trigger == TriggerKind.SITUATION and spoke:
                self._tuner.on_proactive_open()
            if trigger == TriggerKind.UTTERANCE:
                self._tuner.on_user_utterance(text)
```

- `build_brain` 签名增 `tuner=None`，传入 `DecisionHub(...)`。

- [ ] **Step 5: `src/yuki/cognition/agent.py` 装配**

顶部 import 增补：

```python
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.tuner import FeedbackTuner
```

`setup()` 中，`bridge` 构建之后、`build_brain` 调用之前插入：

```python
        policy = DecisionPolicy(
            proactive_cooldown_s=self.config.brain.proactive_cooldown_s,
            proactive_enabled=self.config.brain.proactive_enabled,
        )
        soul = SoulStore(self.config.soul.path, self.config.persona_name)
        tuner = FeedbackTuner(policy, soul)
        tuner.load_soul()
        self._bridge = bridge
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
        )
```

（替换原 `self._bridge = bridge; self._hub = build_brain(... bridge=bridge)` 块。）

- [ ] **Step 6: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py tests/cognition/test_cognition.py tests/cognition/test_tuner.py tests/cognition/test_policy.py -v`
Expected: 全 PASS。

- [ ] **Step 7: 全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（现有 312 passed 不减;新增 soul/tuner/hub 测试）。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: wire FeedbackTuner and soul into DecisionHub and CognitionAgent"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1/3/4；§3 SoulStore（load/save/reset/版本/容错）→ Task 1；§4 FeedbackTuner（窗口/极性/互斥/钳制/soul 写回）→ Task 3；§5 policy.set_cooldown_s → Task 2；§6 hub 接线 → Task 4；§7 配置 → Task 1；§8 测试 → 各任务。
- **一致性**：`FeedbackTuner` 用 `policy.cooldown_s`（Task 2 属性）读初值、`policy.set_cooldown_s` 写回;`COOLDOWN_KEY` 常量在 tuner 定义、soul 仅存字典;hub 的 `_tuner` 通知点(SITUATION spoke/UTTERANCE)与 spec §4 一致;agent 构建的 policy 与 tuner 共享同一实例(传给 build_brain)。
- **e2e 等价**：无反馈时 tuner 不 adjust → 无 soul 文件、冷却=config;awake → `我在,你说。` 不变。
- **测试注意**：tuner 测试中 `make()` 的 `tmp_path` 需经 fixture 传入（`test_initial_cooldown_from_policy` 亦然），避免 `None / "s.json"` 路径错误。
