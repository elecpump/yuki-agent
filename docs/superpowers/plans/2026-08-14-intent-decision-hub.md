# Yuki 意图识别与决策中枢 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Brain 内核——意图/情绪分类 → 决策策略 → 原子动作执行 → 渲染回复,替换 L1Responder,含主动开口决策(固定冷却)。

**Architecture:** 新增 `src/yuki/cognition/brain/` 包:分类器(规则 + 协议接口)→ 策略(意图/触发→动作序列 + 冷却门控)→ 动作执行器(组合成回复文本 + 副动作)→ `DecisionHub` 编排(订阅事件/发布 REPLY/记决策轨迹)。L1Engine 保留为 inform 动作的文本生成源。

**Tech Stack:** Python ≥3.11,stdlib + 现有 pydantic/enum/dataclass。无新增运行时依赖。

## Global Constraints

- 零新增运行时依赖;零协议变更(REPLY 主题与载荷不变);纯 cognition 内部变化。
- `Intent` 枚举 10 类:`chit_chat/emotional/entertainment/game/roleplay/creative/companion/system/safety/unknown`;`Emotion` 枚举 7 类:`neutral/joy/sadness/anxiety/anger/love/tired`。
- 规则分类器:**safety 最先匹配**;关键词子串匹配(文本已小写化);未命中 → `unknown`/`neutral`。规则表数据驱动可注入。
- `DecisionPolicy.decide(trigger, intent, emotion, text, situation, last_open_ts, now)`:
  - UTTERANCE:按 `{intent: [动作名]}` 策略表;safety→`[safety_escalate]` 短路;unknown→`[clarify]`;system 命中告别关键词→`[farewell]` 否则 `[inform]`;emotional/companion 追加 `write_memory` 副动作。
  - AWAKE:→ `[inform]`(L1 生成 `我在，你说。`,e2e 断言不变)。
  - SITUATION:proactive_enabled 关闭→`[stay_silent]`;否则冷却通过且情境非敏感且 topic 非空→`[acknowledge, ask]`,否则→`[stay_silent]`。
- 动作名全集(15):`empathize/acknowledge/comfort/encourage/ask/clarify/inform/joke/story/invite_game/farewell/safety_escalate/write_memory/call_function/stay_silent`。副动作 `write_memory`/`call_function`/`stay_silent` 不产文本。
- `write_memory` 参数:`memory_type="preference"`(emotional/companion 披露)、`content=<utterance>`、`source="brain"`、`metadata={}`。
- 决策轨迹经 `logger.get_decision_logger()` 落盘(`logs/decision.jsonl`)。
- `Config` 新增 `brain:` 节(`YUKI_BRAIN_PROACTIVE_COOLDOWN_S`/`YUKI_BRAIN_PROACTIVE_ENABLED`),默认 120.0 / true。
- 删除 `src/yuki/cognition/l1_responder.py`、`tests/cognition/test_l1_responder.py`;`cognition/agent.py` 用 `build_brain` 替换 `build_l1_responder`,health 组件 `l1` → `brain`。
- 测试命令(仓库根):`& ".venv\Scripts\python.exe" -m pytest <文件> -v`;全仓 `-m pytest`(e2e 默认跳过)。
- 设计文档:`docs/superpowers/specs/2026-08-14-intent-decision-hub-design.md`(已提交)。

---

## 文件结构

**新增**
- `src/yuki/cognition/brain/__init__.py`
- `src/yuki/cognition/brain/classifier.py` — Intent/Emotion 枚举 + 协议 + 规则实现
- `src/yuki/cognition/brain/actions.py` — Action/ActionContext/ActionExecutor + ACTION_EXECUTORS
- `src/yuki/cognition/brain/policy.py` — TriggerKind + DecisionPolicy
- `src/yuki/cognition/brain/hub.py` — DecisionHub + DecisionTrace + build_brain
- `tests/cognition/test_classifier.py`、`tests/cognition/test_policy.py`、`tests/cognition/test_actions.py`、`tests/cognition/test_hub.py`

**修改**
- `src/yuki/config.py`、`config.example.yaml`(brain 节)
- `src/yuki/cognition/agent.py`(build_brain + health brain)
- `tests/cognition/test_cognition.py`(适配接线)
- `tests/test_config.py`(brain 默认值/env)

**删除**
- `src/yuki/cognition/l1_responder.py`、`tests/cognition/test_l1_responder.py`

---

### Task 1: 分类器（Intent/Emotion + 规则实现）

**Files:**
- Create: `src/yuki/cognition/brain/classifier.py`
- Test: `tests/cognition/test_classifier.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Intent`/`Emotion` 枚举;`IntentClassifier`/`EmotionClassifier` 协议;`RuleIntentClassifier(rules: list[tuple[tuple[str,...], Intent]] | None)`、`RuleEmotionClassifier(rules: list[tuple[tuple[str,...], Emotion]] | None)`(均 `classify(text) -> X`)。Task 2/3/4 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_classifier.py`**

```python
import pytest

from yuki.cognition.brain.classifier import (
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)


def test_default_intent_rules_hit():
    clf = RuleIntentClassifier()
    assert clf.classify("你好，在吗") == Intent.CHIT_CHAT
    assert clf.classify("我今天很难过") == Intent.EMOTIONAL
    assert clf.classify("讲个笑话给我听") == Intent.ENTERTAINMENT
    assert clf.classify("成语接龙来不来") == Intent.GAME
    assert clf.classify("扮演我的女朋友") == Intent.ROLEPLAY
    assert clf.classify("帮我写首情诗") == Intent.CREATIVE
    assert clf.classify("睡不着陪我聊聊") == Intent.COMPANION
    assert clf.classify("你能做什么") == Intent.SYSTEM
    assert clf.classify("今天天气怎么样") == Intent.CHIT_CHAT


def test_safety_wins_over_other_intents():
    clf = RuleIntentClassifier()
    assert clf.classify("我很难过，不想活了") == Intent.SAFETY


def test_unknown_fallback():
    clf = RuleIntentClassifier()
    assert clf.classify("qwertyuiop 乱码") == Intent.UNKNOWN
    assert clf.classify("") == Intent.UNKNOWN


def test_case_insensitive_and_injectable_rules():
    clf = RuleIntentClassifier(rules=[(("HELLO",), Intent.CHIT_CHAT)])
    assert clf.classify("Say HELLO world") == Intent.CHIT_CHAT


def test_emotion_classifier():
    clf = RuleEmotionClassifier()
    assert clf.classify("太开心了") == Emotion.JOY
    assert clf.classify("我今天很难过") == Emotion.SADNESS
    assert clf.classify("压力好大") == Emotion.ANXIETY
    assert clf.classify("随便聊聊") == Emotion.NEUTRAL
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_classifier.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/classifier.py`**

```python
from enum import Enum
from typing import Protocol


class Intent(str, Enum):
    CHIT_CHAT = "chit_chat"
    EMOTIONAL = "emotional"
    ENTERTAINMENT = "entertainment"
    GAME = "game"
    ROLEPLAY = "roleplay"
    CREATIVE = "creative"
    COMPANION = "companion"
    SYSTEM = "system"
    SAFETY = "safety"
    UNKNOWN = "unknown"


class Emotion(str, Enum):
    NEUTRAL = "neutral"
    JOY = "joy"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANGER = "anger"
    LOVE = "love"
    TIRED = "tired"


class IntentClassifier(Protocol):
    def classify(self, text: str) -> Intent: ...


class EmotionClassifier(Protocol):
    def classify(self, text: str) -> Emotion: ...


# 顺序即优先级：SAFETY 最先。关键词为子串匹配（文本已小写化）。
DEFAULT_INTENT_RULES: list[tuple[tuple[str, ...], Intent]] = [
    (("自杀", "自伤", "不想活", "想死", "活着没意思", "想结束生命", "割腕"), Intent.SAFETY),
    (("难过", "伤心", "求安慰", "安慰我", "好累", "压力", "焦虑", "失眠", "想哭", "委屈", "孤独", "好烦", "崩溃", "想你了", "抱抱", "升职", "考上"), Intent.EMOTIONAL),
    (("讲笑话", "笑话", "睡前故事", "讲故事", "谜语", "脑筋急转弯", "冷知识", "推荐首歌", "推荐一首歌", "好剧", "电影", "运势", "星座"), Intent.ENTERTAINMENT),
    (("成语接龙", "词语接龙", "海龟汤", "猜数字", "石头剪刀布", "真心话", "大冒险", "剧本杀", "扮演侦探", "井字棋"), Intent.GAME),
    (("扮演", "你是哈利波特", "当我的", "假装你"), Intent.ROLEPLAY),
    (("写首", "写诗", "写词", "续写", "编一个", "起名", "想个"), Intent.CREATIVE),
    (("陪我", "睡不着", "提醒我", "一起学习", "生日", "圣诞", "纪念日"), Intent.COMPANION),
    (("你能做什么", "你会什么", "温柔一点", "凶一点", "回答得", "再见", "晚安", "拜拜", "下次聊", "投诉"), Intent.SYSTEM),
    (("你好", "您好", "在吗", "早上好", "下午好", "晚上好", "嗨", "哈喽", "你叫什么", "你多大了", "在干嘛", "天气", "聊聊", "最近"), Intent.CHIT_CHAT),
]


class RuleIntentClassifier:
    def __init__(self, rules: list[tuple[tuple[str, ...], Intent]] | None = None) -> None:
        self._rules = rules if rules is not None else DEFAULT_INTENT_RULES

    def classify(self, text: str) -> Intent:
        lowered = (text or "").lower()
        for keywords, intent in self._rules:
            if any(kw in lowered for kw in keywords):
                return intent
        return Intent.UNKNOWN


DEFAULT_EMOTION_RULES: list[tuple[tuple[str, ...], Emotion]] = [
    (("开心", "高兴", "好棒", "太棒了", "升职", "考上", "哈哈", "真棒", "耶"), Emotion.JOY),
    (("难过", "伤心", "想哭", "委屈", "失落", "哭了"), Emotion.SADNESS),
    (("焦虑", "紧张", "压力", "害怕", "担心", "不安"), Emotion.ANXIETY),
    (("生气", "气死", "烦死了", "讨厌", "恼火"), Emotion.ANGER),
    (("想你", "爱你", "喜欢你", "抱抱"), Emotion.LOVE),
    (("好累", "累死", "疲惫", "困"), Emotion.TIRED),
]


class RuleEmotionClassifier:
    def __init__(self, rules: list[tuple[tuple[str, ...], Emotion]] | None = None) -> None:
        self._rules = rules if rules is not None else DEFAULT_EMOTION_RULES

    def classify(self, text: str) -> Emotion:
        lowered = (text or "").lower()
        for keywords, emotion in self._rules:
            if any(kw in lowered for kw in keywords):
                return emotion
        return Emotion.NEUTRAL
```

- [ ] **Step 4: 创建 `src/yuki/cognition/brain/__init__.py`(Task 1 阶段先空文件,Task 5 再补导出)**

```python
# 空文件。Task 5 完成后补导出。
```

- [ ] **Step 5: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_classifier.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/brain/classifier.py src/yuki/cognition/brain/__init__.py tests/cognition/test_classifier.py
git commit -m "feat: add intent and emotion classifiers"
```

---

### Task 2: 决策策略（TriggerKind + DecisionPolicy）

**Files:**
- Create: `src/yuki/cognition/brain/policy.py`
- Test: `tests/cognition/test_policy.py`

**Interfaces:**
- Consumes: `Intent`/`Emotion`（Task 1）、`Action`（Task 3 定义——本任务仅用 `Action` dataclass 名字;若 Task 3 未完成,`Action` 由 Task 3 定义,本任务测试在 Task 3 后运行。为解耦,Task 2 先依赖 actions.py 的 `Action`——需在 Task 2 Step 3 同时创建 `actions.py` 中的 `Action` dataclass;完整执行器在 Task 3）。
- Produces: `TriggerKind(Enum)`、`FAREWELL_KEYWORDS`、`DEFAULT_POLICY_TABLE`、`DecisionPolicy(proactive_cooldown_s, *, proactive_enabled=True, policy_table=None)` 方法 `decide(trigger, intent, emotion, text="", situation=None, last_open_ts=None, now=0.0) -> list[Action]`。Task 4 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_policy.py`**

```python
import pytest

from yuki.cognition.brain.actions import Action
from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.brain.policy import DecisionPolicy, TriggerKind


def test_utterance_intent_to_actions():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.CHIT_CHAT, Emotion.NEUTRAL, text="你好")] == ["inform"]
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.EMOTIONAL, Emotion.SADNESS, text="我很难过")] == ["empathize", "ask", "write_memory"]
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.UNKNOWN, Emotion.NEUTRAL, text="乱码")] == ["clarify"]


def test_safety_short_circuits_and_skips_write_memory():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.UTTERANCE, Intent.SAFETY, Emotion.SADNESS, text="我不想活了")
    assert [a.name for a in actions] == ["safety_escalate"]


def test_system_farewell_disambiguation():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.SYSTEM, Emotion.NEUTRAL, text="再见啦")] == ["farewell"]
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.SYSTEM, Emotion.NEUTRAL, text="你能做什么")] == ["inform"]


def test_disclosure_write_memory_params():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.UTTERANCE, Intent.COMPANION, Emotion.JOY, text="我今天升职了")
    wm = [a for a in actions if a.name == "write_memory"][0]
    assert wm.params["memory_type"] == "preference"
    assert wm.params["content"] == "我今天升职了"


def test_awake_returns_inform():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert [a.name for a in policy.decide(TriggerKind.AWAKE, Intent.UNKNOWN, Emotion.NEUTRAL)] == ["inform"]


def test_situation_stay_silent_when_disabled():
    policy = DecisionPolicy(120.0, proactive_enabled=False)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算", "sensitive": False}, last_open_ts=0.0, now=999.0)
    assert [a.name for a in actions] == ["stay_silent"]


def test_situation_proactive_when_cooldown_passed():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算", "sensitive": False}, last_open_ts=100.0, now=300.0)
    assert [a.name for a in actions] == ["acknowledge", "ask"]


def test_situation_stay_silent_within_cooldown():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算", "sensitive": False}, last_open_ts=200.0, now=300.0)
    assert [a.name for a in actions] == ["stay_silent"]


def test_situation_stay_silent_when_sensitive_or_no_topic():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert [a.name for a in policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                                          situation={"topic": "x", "sensitive": True}, last_open_ts=0.0, now=999.0)] == ["stay_silent"]
    assert [a.name for a in policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                                          situation={"topic": "", "sensitive": False}, last_open_ts=0.0, now=999.0)] == ["stay_silent"]
    assert [a.name for a in policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                                          situation=None, last_open_ts=0.0, now=999.0)] == ["stay_silent"]


def test_policy_table_injectable():
    policy = DecisionPolicy(120.0, policy_table={Intent.GAME: ["invite_game"]})
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.GAME, Emotion.NEUTRAL, text="猜数字")] == ["invite_game"]
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.actions'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/brain/actions.py`(本任务仅 Action dataclass;执行器 Task 3 追加)**

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Action:
    name: str
    params: dict = field(default_factory=dict)
```

- [ ] **Step 4: 创建 `src/yuki/cognition/brain/policy.py`**

```python
from enum import Enum

from yuki.cognition.brain.actions import Action
from yuki.cognition.brain.classifier import Emotion, Intent


class TriggerKind(str, Enum):
    UTTERANCE = "utterance"
    AWAKE = "awake"
    SITUATION = "situation"


FAREWELL_KEYWORDS = ("再见", "晚安", "拜拜", "下次聊")

# 披露类意图（emotional/companion）追加 write_memory 副动作
DISCLOSURE_INTENTS = (Intent.EMOTIONAL, Intent.COMPANION)

DEFAULT_POLICY_TABLE: dict[Intent, list[str]] = {
    Intent.CHIT_CHAT: ["inform"],
    Intent.EMOTIONAL: ["empathize", "ask"],
    Intent.ENTERTAINMENT: ["joke"],
    Intent.GAME: ["invite_game"],
    Intent.ROLEPLAY: ["inform"],
    Intent.CREATIVE: ["inform"],
    Intent.COMPANION: ["acknowledge", "ask"],
    Intent.SYSTEM: ["inform"],
    Intent.SAFETY: ["safety_escalate"],
    Intent.UNKNOWN: ["clarify"],
}


class DecisionPolicy:
    """意图/触发 → 动作序列。UTTERANCE 按策略表;SITUATION 走主动开口冷却门控。"""

    def __init__(
        self,
        proactive_cooldown_s: float,
        *,
        proactive_enabled: bool = True,
        policy_table: dict[Intent, list[str]] | None = None,
    ) -> None:
        self._cooldown = proactive_cooldown_s
        self._enabled = proactive_enabled
        self._table = policy_table if policy_table is not None else DEFAULT_POLICY_TABLE

    def decide(
        self,
        trigger: TriggerKind,
        intent: Intent,
        emotion: Emotion,
        text: str = "",
        situation: dict | None = None,
        last_open_ts: float | None = None,
        now: float = 0.0,
    ) -> list[Action]:
        if trigger == TriggerKind.AWAKE:
            return [Action("inform")]
        if trigger == TriggerKind.SITUATION:
            return self._decide_situation(situation, last_open_ts, now)
        return self._decide_utterance(intent, text)

    def _decide_utterance(self, intent: Intent, text: str) -> list[Action]:
        if intent == Intent.SAFETY:
            return [Action("safety_escalate")]
        if intent == Intent.SYSTEM and any(kw in text for kw in FAREWELL_KEYWORDS):
            return [Action("farewell")]
        names = self._table.get(intent, ["inform"])
        actions = [Action(name) for name in names]
        if intent in DISCLOSURE_INTENTS:
            actions.append(Action("write_memory", {
                "memory_type": "preference",
                "content": text,
            }))
        return actions

    def _decide_situation(
        self,
        situation: dict | None,
        last_open_ts: float | None,
        now: float,
    ) -> list[Action]:
        if not self._enabled:
            return [Action("stay_silent")]
        if situation is None or situation.get("sensitive") or not situation.get("topic"):
            return [Action("stay_silent")]
        if last_open_ts is not None and now - last_open_ts < self._cooldown:
            return [Action("stay_silent")]
        return [Action("acknowledge", {"topic": situation.get("topic")}), Action("ask")]
```

- [ ] **Step 5: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_policy.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/brain/actions.py src/yuki/cognition/brain/policy.py tests/cognition/test_policy.py
git commit -m "feat: add decision policy with cooldown-gated proactive actions"
```

---

### Task 3: 动作执行器（ACTION_EXECUTORS）

**Files:**
- Modify: `src/yuki/cognition/brain/actions.py`（追加 ActionContext/ActionExecutor + 执行器 + ACTION_EXECUTORS）
- Test: `tests/cognition/test_actions.py`

**Interfaces:**
- Consumes: `Intent`/`Emotion`（Task 1）、`Action`（Task 2）、`L1Engine`、`MemoryManager`、`FunctionRegistry`（已有模块）。
- Produces: `ActionContext`(intent/emotion/text/situation/memory/registry/l1)、`ActionExecutor` 协议、`ACTION_EXECUTORS: dict[str, ActionExecutor]`(15 个动作名)。Task 4 依赖。

- [ ] **Step 1: 写失败测试 `tests/cognition/test_actions.py`**

```python
import pytest

from yuki.cognition.brain.actions import ACTION_EXECUTORS, Action, ActionContext
from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore


class FakeL1:
    def reply(self, text, context=None):
        return f"l1:{text}" if text else "我在，你说。"


@pytest.fixture()
def ctx():
    return ActionContext(intent=Intent.UNKNOWN, emotion=Emotion.NEUTRAL, text="你好", situation=None)


def test_all_action_names_have_executors():
    expected = {
        "empathize", "acknowledge", "comfort", "encourage", "ask", "clarify",
        "inform", "joke", "story", "invite_game", "farewell",
        "safety_escalate", "write_memory", "call_function", "stay_silent",
    }
    assert set(ACTION_EXECUTORS) == expected


def test_empathize_uses_emotion(ctx):
    ctx.emotion = Emotion.SADNESS
    assert ACTION_EXECUTORS["empathize"](Action("empathize"), ctx) != ""
    ctx.emotion = Emotion.JOY
    assert "开心" in ACTION_EXECUTORS["empathize"](Action("empathize"), ctx)


def test_ask_injects_situation_topic():
    c = ActionContext(intent=Intent.UNKNOWN, emotion=Emotion.NEUTRAL, text="",
                      situation={"topic": "量子计算"})
    assert "量子计算" in ACTION_EXECUTORS["ask"](Action("ask"), c)


def test_inform_uses_l1():
    c = ActionContext(intent=Intent.CHIT_CHAT, emotion=Emotion.NEUTRAL, text="你好", l1=FakeL1())
    assert ACTION_EXECUTORS["inform"](Action("inform"), c) == "l1:你好"


def test_joke_falls_back_to_placeholder():
    out = ACTION_EXECUTORS["joke"](Action("joke", {"items": []}), ctx)
    assert "还在学" in out


def test_write_memory_is_side_effect_only(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    c = ActionContext(intent=Intent.EMOTIONAL, emotion=Emotion.SADNESS, text="我今天升职了",
                      memory=memory)
    text = ACTION_EXECUTORS["write_memory"](Action("write_memory", {
        "memory_type": "preference", "content": "我今天升职了"}), c)
    assert text == ""
    results = memory.query("升职")
    assert results and results[0]["content"] == "我今天升职了"


def test_call_function_dispatches_when_registry_present():
    registry = FunctionRegistry()
    registry.tool("echo", description="e", params=None)(lambda p: "ok")
    c = ActionContext(intent=Intent.SYSTEM, emotion=Emotion.NEUTRAL, text="",
                      registry=registry)
    text = ACTION_EXECUTORS["call_function"](Action("call_function", {
        "name": "echo", "arguments": {}}), c)
    assert text == ""


def test_stay_silent_no_text(ctx):
    assert ACTION_EXECUTORS["stay_silent"](Action("stay_silent"), ctx) == ""


def test_safety_escalate_mentions_help(ctx):
    out = ACTION_EXECUTORS["safety_escalate"](Action("safety_escalate"), ctx)
    assert "求助" in out
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_actions.py -v`
Expected: FAIL（`ImportError: cannot import name 'ACTION_EXECUTORS'`）。

- [ ] **Step 3: 重写 `src/yuki/cognition/brain/actions.py`（在 Action 后追加）**

```python
from dataclasses import dataclass, field
from typing import Protocol

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.l1 import L1Engine
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager


@dataclass(frozen=True)
class Action:
    name: str
    params: dict = field(default_factory=dict)


@dataclass
class ActionContext:
    intent: Intent
    emotion: Emotion
    text: str = ""
    situation: dict | None = None
    memory: MemoryManager | None = None
    registry: FunctionRegistry | None = None
    l1: L1Engine | None = None


class ActionExecutor(Protocol):
    def __call__(self, action: Action, ctx: ActionContext) -> str: ...


DEFAULT_JOKES = ("为什么程序员分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。", "我不冷，我只是穿得少。")
DEFAULT_STORIES = ("从前有座山，山里有个庙，庙里有个老和尚在讲故事……")


def _empathize(action: Action, ctx: ActionContext) -> str:
    templates = {
        Emotion.SADNESS: "听起来你今天不太开心，我一直都在。",
        Emotion.ANXIETY: "别太紧张，我们慢慢来，我陪着你。",
        Emotion.ANGER: "听起来让你很生气，想说说怎么回事吗？",
        Emotion.LOVE: "我也想你呀，一直在呢。",
        Emotion.TIRED: "辛苦了，今天是不是很累？",
        Emotion.JOY: "太好啦，替你开心！",
        Emotion.NEUTRAL: "嗯，我在认真听。",
    }
    return templates.get(ctx.emotion, templates[Emotion.NEUTRAL])


def _acknowledge(action: Action, ctx: ActionContext) -> str:
    topic = action.params.get("topic") or (ctx.situation or {}).get("topic")
    if topic:
        return f"嗯，你正在看{topic}。"
    return "嗯嗯。"


def _comfort(action: Action, ctx: ActionContext) -> str:
    return "抱抱你，不管怎样都有我陪着你。"


def _encourage(action: Action, ctx: ActionContext) -> str:
    return "我相信你可以的，慢慢来。"


def _ask(action: Action, ctx: ActionContext) -> str:
    topic = (ctx.situation or {}).get("topic")
    if topic:
        return f"关于{topic}，你更想聊哪方面？"
    return "然后呢？"


def _clarify(action: Action, ctx: ActionContext) -> str:
    return "嗯？我可能没太听懂，你能再说一遍吗？"


def _inform(action: Action, ctx: ActionContext) -> str:
    if ctx.l1 is not None:
        return ctx.l1.reply(ctx.text or "", context=ctx.situation)
    return "嗯嗯，我在听。"


def _joke(action: Action, ctx: ActionContext) -> str:
    items = action.params.get("items") or DEFAULT_JOKES
    return items[0] if items else "这个我还在学，先陪你聊点别的吧。"


def _story(action: Action, ctx: ActionContext) -> str:
    items = action.params.get("items") or DEFAULT_STORIES
    return items[0] if items else "这个我还在学，先陪你聊点别的吧。"


def _invite_game(action: Action, ctx: ActionContext) -> str:
    return "要不要玩个成语接龙？我先来：一心一意。"


def _farewell(action: Action, ctx: ActionContext) -> str:
    return "好，再见啦，随时找我。"


def _safety_escalate(action: Action, ctx: ActionContext) -> str:
    return ("我在。你现在还好吗？如果很难受，请一定向身边信任的人求助，"
            "或者拨打心理援助热线。不要一个人扛着，我一直陪着你。")


def _write_memory(action: Action, ctx: ActionContext) -> str:
    if ctx.memory is not None:
        ctx.memory.write(
            action.params.get("memory_type", "scenario"),
            action.params.get("content", ctx.text or ""),
            source="brain",
            sensitivity=action.params.get("sensitivity", 0),
            metadata=action.params.get("metadata", {}),
        )
    return ""


def _call_function(action: Action, ctx: ActionContext) -> str:
    if ctx.registry is not None:
        name = action.params.get("name", "")
        args = action.params.get("arguments", {})
        try:
            ctx.registry.call(name, args)
        except Exception:
            pass
    return ""


def _stay_silent(action: Action, ctx: ActionContext) -> str:
    return ""


ACTION_EXECUTORS: dict[str, ActionExecutor] = {
    "empathize": _empathize,
    "acknowledge": _acknowledge,
    "comfort": _comfort,
    "encourage": _encourage,
    "ask": _ask,
    "clarify": _clarify,
    "inform": _inform,
    "joke": _joke,
    "story": _story,
    "invite_game": _invite_game,
    "farewell": _farewell,
    "safety_escalate": _safety_escalate,
    "write_memory": _write_memory,
    "call_function": _call_function,
    "stay_silent": _stay_silent,
}
```

- [ ] **Step 4: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_actions.py tests/cognition/test_policy.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/actions.py tests/cognition/test_actions.py
git commit -m "feat: add atomic action executors"
```

---

### Task 4: 决策中枢（DecisionHub + brain 配置）

**Files:**
- Create: `src/yuki/cognition/brain/hub.py`
- Modify: `src/yuki/config.py`、`config.example.yaml`、`tests/test_config.py`
- Test: `tests/cognition/test_hub.py`

**Interfaces:**
- Consumes: 分类器（Task 1）、`Action`（Task 2）、`ACTION_EXECUTORS`/`ActionContext`（Task 3）、`DecisionPolicy`/`TriggerKind`（Task 2）、`L1Engine`、`MemoryManager`、`FunctionRegistry`、`get_decision_logger`。
- Produces: `DecisionTrace`(to_dict)、`DecisionHub(bus, *, intent_clf=None, emotion_clf=None, policy=None, memory=None, registry=None, l1=None, executors=None, trace_logger=None)` 方法 `on_awake/on_user_utterance/on_situation_update`;`build_brain(bus, *, memory=None, registry=None, config=None, intent_clf=None, emotion_clf=None, policy=None) -> DecisionHub`(订阅三个主题)。`Config.brain`(BrainConfig: proactive_cooldown_s=120.0, proactive_enabled=True)。Task 5 依赖。

- [ ] **Step 1: 追加 brain 配置测试到 `tests/test_config.py`**

```python
def test_brain_defaults():
    config = Config()
    assert config.brain.proactive_cooldown_s == 120.0
    assert config.brain.proactive_enabled is True


def test_brain_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_BRAIN_PROACTIVE_COOLDOWN_S", "60.0")
    monkeypatch.setenv("YUKI_BRAIN_PROACTIVE_ENABLED", "false")
    config = Config.load(None)
    assert config.brain.proactive_cooldown_s == 60.0
    assert config.brain.proactive_enabled is False
```

- [ ] **Step 2: 写失败测试 `tests/cognition/test_hub.py`**

```python
import pytest

from yuki.cognition.brain.actions import Action
from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.brain.hub import DecisionHub, build_brain
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return "我在，你说。" if not text else f"l1:{text}"


@pytest.fixture()
def hub(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(
        bus,
        policy=DecisionPolicy(proactive_cooldown_s=120.0),
        memory=memory,
        l1=FakeL1(),
    )
    yield hub, bus, memory
    memory.close()


def _reply_text(bus) -> str | None:
    for topic, payload in reversed(bus.published):
        if topic == Topics.REPLY:
            return payload["text"]
    return None


def test_awake_replies_l1_greeting(hub):
    h, bus, _ = hub
    h.on_awake(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert _reply_text(bus) == "我在，你说。"


def test_chit_chat_utterance_replies(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus) == "l1:你好"


def test_emotional_utterance_empathizes_and_writes_memory(hub):
    h, bus, memory = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我今天升职了", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert "开心" in text or "替你开心" in text
    assert memory.query("升职")


def test_unknown_utterance_clarifies(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "qwerty乱码", "duration_s": 1.0, "ts": 0.0})
    assert "听懂" in _reply_text(bus)


def test_safety_utterance_escalates(hub):
    h, bus, memory = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我不想活了", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert "求助" in text
    assert memory.all() == []  # safety 不写记忆


def test_situation_within_cooldown_stays_silent(hub):
    h, bus, _ = hub
    h.on_awake(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})  # 记录 last_open
    before = len(bus.published)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert len(bus.published) == before  # 无新 REPLY


def test_situation_proactive_after_cooldown(hub, monkeypatch):
    h, bus, _ = hub
    import time
    monkeypatch.setattr("time.time", lambda: 0.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    monkeypatch.setattr("time.time", lambda: 200.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    text = _reply_text(bus)
    assert "量子计算" in text


def test_build_brain_subscribes_and_configures(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = build_brain(bus, memory=memory, registry=FunctionRegistry())
    assert Topics.AWAKE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    memory.close()


def test_decision_trace_logged(hub):
    h, bus, _ = hub
    records = []
    h._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert records and records[0]["trigger"] == "utterance"
    assert records[0]["intent"] == "chit_chat"
```

- [ ] **Step 3: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.brain.hub'`）。

- [ ] **Step 4: 创建 `src/yuki/cognition/brain/hub.py`**

```python
import time

from yuki.cognition.brain.actions import ACTION_EXECUTORS, Action, ActionContext
from yuki.cognition.brain.classifier import (
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)
from yuki.cognition.brain.policy import DecisionPolicy, TriggerKind
from yuki.cognition.l1 import L1Engine
from yuki.logger import get_decision_logger
from yuki.topics import Topics


class DecisionTrace:
    def __init__(self, *, trigger, intent, emotion, actions, rendered, reason, cooldown_state) -> None:
        self.trigger = trigger
        self.intent = intent
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.cooldown_state = cooldown_state

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "intent": self.intent,
            "emotion": self.emotion,
            "actions": [a.name for a in self.actions],
            "rendered": self.rendered,
            "reason": self.reason,
            "cooldown_state": self.cooldown_state,
        }


class DecisionHub:
    """Brain 内核：分类 → 决策 → 执行 → 渲染 → 发布 REPLY + 决策轨迹。"""

    def __init__(self, bus, *, intent_clf=None, emotion_clf=None, policy=None,
                 memory=None, registry=None, l1=None, executors=None, trace_logger=None) -> None:
        self._bus = bus
        self._intent_clf = intent_clf or RuleIntentClassifier()
        self._emotion_clf = emotion_clf or RuleEmotionClassifier()
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._l1 = l1 or L1Engine()
        self._executors = executors if executors is not None else ACTION_EXECUTORS
        self._trace_logger = trace_logger or get_decision_logger()
        self._context = None
        self._last_open_ts = None

    def on_situation_update(self, topic: str, payload: dict) -> None:
        self._context = payload
        self._handle(TriggerKind.SITUATION, "", situation=payload)

    def on_awake(self, topic: str, payload: dict) -> None:
        self._handle(TriggerKind.AWAKE, "")

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        self._handle(TriggerKind.UTTERANCE, text)

    def _handle(self, trigger: TriggerKind, text: str, situation: dict | None = None) -> None:
        intent = Intent.UNKNOWN
        emotion = Emotion.NEUTRAL
        if trigger == TriggerKind.UTTERANCE:
            intent = self._intent_clf.classify(text)
            emotion = self._emotion_clf.classify(text)
        actions = self._policy.decide(
            trigger, intent, emotion, text=text, situation=situation or self._context,
            last_open_ts=self._last_open_ts, now=time.time(),
        )
        rendered, spoke = self._execute(actions, intent, emotion, text, situation or self._context)
        reason = "spoke" if spoke else "silent"
        if spoke:
            self._last_open_ts = time.time()
            self._bus.publish(Topics.REPLY, {"text": rendered, "ts": time.time()})
        self._trace_logger.info("decision", **DecisionTrace(
            trigger=trigger.value, intent=intent.value, emotion=emotion.value,
            actions=actions, rendered=rendered, reason=reason,
            cooldown_state={"last_open_ts": self._last_open_ts},
        ).to_dict())

    def _execute(self, actions, intent, emotion, text, situation):
        ctx = ActionContext(intent=intent, emotion=emotion, text=text,
                            situation=situation, memory=self._memory,
                            registry=self._registry, l1=self._l1)
        fragments = []
        for action in actions:
            executor = self._executors.get(action.name)
            if executor is None:
                continue
            fragments.append(executor(action, ctx))
        rendered = " ".join(f for f in fragments if f)
        return rendered, bool(rendered)


def build_brain(bus, *, memory=None, registry=None, config=None,
                intent_clf=None, emotion_clf=None, policy=None) -> DecisionHub:
    from yuki.config import Config
    cfg = config or Config.from_env()
    hub = DecisionHub(
        bus,
        intent_clf=intent_clf,
        emotion_clf=emotion_clf,
        policy=policy or DecisionPolicy(
            proactive_cooldown_s=cfg.brain.proactive_cooldown_s,
            proactive_enabled=cfg.brain.proactive_enabled,
        ),
        memory=memory,
        registry=registry,
    )
    bus.subscribe(Topics.AWAKE, hub.on_awake)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
```

- [ ] **Step 5: `src/yuki/config.py` 加 BrainConfig 并注册**

在 `HealthConfig` 之后新增：

```python
class BrainConfig(BaseModel):
    proactive_cooldown_s: float = Field(120.0, ge=0.0)
    proactive_enabled: bool = True
```

在 `Config` 中 `memory` 字段之后新增：

```python
    brain: BrainConfig = Field(default_factory=BrainConfig)
```

在 `Config.load` 的 section 元组中 `("memory", MemoryConfig),` 之后新增：

```python
            ("brain", BrainConfig),
```

- [ ] **Step 6: `config.example.yaml` 加 brain 节**

```yaml
brain:
  proactive_cooldown_s: 120.0
  proactive_enabled: true
```

- [ ] **Step 7: 运行验证通过**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_hub.py tests/test_config.py -v`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add src/yuki/cognition/brain/hub.py src/yuki/config.py config.example.yaml tests/cognition/test_hub.py tests/test_config.py
git commit -m "feat: add DecisionHub with trace and brain config"
```

---

### Task 5: 接线 CognitionAgent + 删除 L1Responder

**Files:**
- Modify: `src/yuki/cognition/agent.py`、`tests/cognition/test_cognition.py`
- Create: `src/yuki/cognition/brain/__init__.py`（补导出）
- Delete: `src/yuki/cognition/l1_responder.py`、`tests/cognition/test_l1_responder.py`

**Interfaces:**
- Consumes: `build_brain`/`DecisionHub`（Task 4）、`MemoryManager`/`MemoryStore`、`FunctionRegistry`/`register_builtin_system`。
- Produces: `CognitionAgent.__init__` 新增 `memory`/`registry` 注入;`setup()` 用 `build_brain`;`health_components()` 的 `l1` → `brain`。`src/yuki/cognition/brain/__init__.py` 导出 `DecisionHub`/`build_brain`/`DecisionPolicy`/`TriggerKind`/`Intent`/`Emotion`/`Action`/分类器。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_cognition.py`（先红）**

```python
def test_cognition_agent_health_includes_brain(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()  # hub 在 setup 中构建，必须先 setup 再查健康
    try:
        components = agent.health_components()
        assert "brain" in components
        assert components["brain"]().ok is True
    finally:
        agent.teardown()
```

- [ ] **Step 2: 运行验证失败**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/cognition/test_cognition.py -v`
Expected: FAIL（`AttributeError: 'CognitionAgent' object has no attribute '_hub'` 或 health 无 `brain`）。

- [ ] **Step 3: 重写 `src/yuki/cognition/agent.py`**

```python
from yuki.cognition.brain.hub import build_brain
from yuki.cognition.l1 import L1Engine
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.functions.registry import FunctionRegistry
from yuki.health import HealthStatus
from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.process import ProcessAgent
from yuki.functions.system import register_builtin_system


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None,
                 memory: MemoryManager | None = None,
                 registry: FunctionRegistry | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._memory = memory
        self._registry = registry
        self._hub = None

    def setup(self) -> None:
        if self._pipeline is None:
            self._pipeline = build_pipeline(
                self.bus,
                vlm=self._vlm,
                sensitive_filter=self._sensitive_filter,
                stt=self._stt,
                frame_client=self._frame_client,
                speech_buffer=self._speech_buffer,
            )
        self._pipeline.warmup_vlm()
        if self._memory is None:
            self._memory = MemoryManager(
                MemoryStore(self.config.memory.db_path),
                decay_base=self.config.memory.decay_base,
                decay_lambda=self.config.memory.decay_lambda,
                decay_threshold=self.config.memory.decay_threshold,
            )
        register_memory_services(self.bus, self._memory)
        if self._registry is None:
            self._registry = FunctionRegistry()
            register_builtin_system(self._registry)
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=None,
        )

    def teardown(self) -> None:
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "brain": self._health_brain,
            "pipeline": self._health_pipeline,
            "memory": self._health_memory,
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(False, {"reason": "no_vlm"})
        return HealthStatus(vlm._loaded, {"loaded": vlm._loaded})

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        return HealthStatus(stt is not None, {"installed": stt is not None})

    def _health_brain(self) -> HealthStatus:
        return HealthStatus(self._hub is not None, {"installed": self._hub is not None})

    def _health_pipeline(self) -> HealthStatus:
        frame_client = getattr(self._pipeline, "_frame_client", None) if self._pipeline else None
        ok = frame_client is not None and hasattr(frame_client, "get_latest")
        return HealthStatus(ok, {"frame_client_available": ok})

    def _health_memory(self) -> HealthStatus:
        ok = self._memory is not None and self._memory.ping()
        return HealthStatus(ok, {"db": self.config.memory.db_path})
```

- [ ] **Step 4: 更新 `src/yuki/cognition/brain/__init__.py`**

```python
from yuki.cognition.brain.actions import Action, ActionContext, ACTION_EXECUTORS  # noqa: F401
from yuki.cognition.brain.classifier import (  # noqa: F401
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)
from yuki.cognition.brain.hub import DecisionHub, build_brain  # noqa: F401
from yuki.cognition.brain.policy import DecisionPolicy, TriggerKind  # noqa: F401
```

- [ ] **Step 5: 删除 l1_responder 及其测试**

Run: `Remove-Item src/yuki/cognition/l1_responder.py, tests/cognition/test_l1_responder.py`

- [ ] **Step 6: 更新 `tests/cognition/test_cognition.py` 既有测试**

- 既有 `test_cognition_agent_wires_pipeline_responder_and_memory`：构造时去掉 `l1=FakeL1()` 参数（brain 内部自建 L1，agent 的 `l1` 注入保留但测试不再需要），断言不变（三个订阅 + MEMORY_SERVICES）。
- 既有 `test_cognition_agent_health_includes_memory`：保留不变（memory 注入，无需 setup）。
- 若文件顶部 import 了 `L1Responder`/`build_l1_responder`，删除对应 import。
- 全文件 grep 确认无 `l1_responder` 引用：`grep -r "l1_responder" src tests` 应仅剩 git 历史。

- [ ] **Step 7: 运行全仓回归**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: 全 PASS（删除 2 个 l1_responder 测试,新增 brain 测试;总数较此前略增）。若有失败,先查是否仍引用 `l1_responder`/`build_l1_responder`:`grep -r "l1_responder\|build_l1_responder" src tests`。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: wire DecisionHub into CognitionAgent, replace L1Responder"
```

---

## 自检记录

- **Spec 覆盖**：§2 文件布局 → Task 1-5；§3 分类器（10 intent/7 emotion/safety 优先/接口）→ Task 1；§4 动作空间（15 动作/组合/副动作）→ Task 3；§5 策略（UTTERANCE 表/safety 短路/system 分流/AWAKE/SITUATION 冷却门控）→ Task 2；§6 hub（订阅/流程/轨迹/记忆直连/函数经 registry）→ Task 4；§7 配置 → Task 4；§8 测试与 e2e 等价 → Task 1-5。
- **一致性**：`Action` 在 Task 2 定义、Task 3 扩展执行器、Task 4 由 hub 消费;`write_memory` 参数（memory_type/content/source）在 policy(Task 2) 生成、executor(Task 3) 消费,约定一致;health 键 `l1`→`brain` 与 Task 5 一致;`Config.brain` 字段名与 env `YUKI_BRAIN_*` 一致。
- **e2e 等价**：awake → `[inform]` → L1.reply("") → `我在,你说。` 不变;REPLY 主题不变。
