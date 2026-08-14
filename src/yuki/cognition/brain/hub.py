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
