import time
import threading

from yuki.cognition.brain.actions import ACTION_EXECUTORS, ActionContext
from yuki.cognition.brain.classifier import (
    Emotion,
    Intent,
    RuleEmotionClassifier,
    RuleIntentClassifier,
)
from yuki.cognition.brain.policy import DecisionPolicy, Tier, TriggerKind
from yuki.cognition.l1 import L1Engine
from yuki.cognition.sensitive import SensitiveFilter
from yuki.logger import get_audit_logger, get_decision_logger, get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.brain.hub")

L2_UNAVAILABLE_NOTICE = "（云端暂时不可用，我先用本地模式陪你。）"


class DecisionTrace:
    def __init__(self, *, ts, trigger, intent, emotion, actions, rendered, reason,
                 tier, cooldown_state) -> None:
        self.ts = ts
        self.trigger = trigger
        self.intent = intent
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.tier = tier
        self.cooldown_state = cooldown_state

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "intent": self.intent,
            "emotion": self.emotion,
            "actions": [a.name for a in self.actions],
            "rendered": self.rendered,
            "reason": self.reason,
            "tier": self.tier,
            "cooldown_state": self.cooldown_state,
        }


class DecisionHub:
    """Brain 内核：分类 → tier 路由（L2 云桥 / L1 动作链）→ 执行 → 发布 REPLY + 轨迹。"""

    def __init__(self, bus, *, intent_clf=None, emotion_clf=None, policy=None,
                 memory=None, registry=None, l1=None, executors=None, trace_logger=None,
                 bridge=None, tuner=None, sensitive_filter=None, audit_logger=None) -> None:
        self._bus = bus
        self._intent_clf = intent_clf or RuleIntentClassifier()
        self._emotion_clf = emotion_clf or RuleEmotionClassifier()
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._l1 = l1 or L1Engine()
        self._executors = executors if executors is not None else ACTION_EXECUTORS
        self._trace_logger = trace_logger or get_decision_logger()
        self._audit_logger = audit_logger or get_audit_logger()
        self._bridge = bridge
        self._tuner = tuner
        self._sensitive_filter = sensitive_filter or SensitiveFilter()
        self._decision_lock = threading.Lock()
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
        # SUB worker 会把不同 topic 的 handler 派发到不同线程；
        # 决策状态（context/last_open_ts）必须串行。
        with self._decision_lock:
            self._handle_locked(trigger, text, situation)

    def _handle_locked(
        self, trigger: TriggerKind, text: str, situation: dict | None = None
    ) -> None:
        intent = Intent.UNKNOWN
        emotion = Emotion.NEUTRAL
        tier = Tier.L1
        if trigger == TriggerKind.UTTERANCE:
            intent = self._intent_clf.classify(text)
            emotion = self._emotion_clf.classify(text)
            tier = self._policy.tier_for(intent)

        actions: list = []
        rendered, spoke, reason = "", False, "silent"
        l2_wanted = tier == Tier.L2
        l2_unavailable = l2_wanted and self._bridge is None
        if l2_wanted and self._bridge is not None:
            rendered, spoke, l2_failed = self._try_l2(text, situation or self._context)
            l2_unavailable = l2_failed
            if spoke:
                reason = "l2"
        if not spoke:
            actions = self._policy.decide(
                trigger, intent, emotion, text=text, situation=situation or self._context,
                last_open_ts=self._last_open_ts, now=time.time(),
            )
            rendered, spoke = self._execute(
                actions, intent, emotion, text, situation or self._context
            )
            reason = "l1" if spoke else "silent"
            if l2_unavailable:
                # §8.2：云端不可用时明确告知用户正在降级到本地，避免"哑巴"。
                rendered = f"{rendered}{L2_UNAVAILABLE_NOTICE}" if spoke else L2_UNAVAILABLE_NOTICE
                spoke = True
                reason = "l2_unavailable_fallback"
        if spoke:
            self._last_open_ts = time.time()
            self._bus.publish(Topics.REPLY, {"text": rendered, "ts": time.time()})

        if trigger == TriggerKind.UTTERANCE and self._memory is not None:
            if text and not self._sensitive_filter.is_sensitive(text):
                self._memory.short_term_add(text, kind="user")
            if spoke and rendered:
                self._memory.short_term_add(rendered, kind="assistant")
        if self._tuner is not None:
            if trigger == TriggerKind.SITUATION and spoke:
                self._tuner.on_proactive_open()
            if trigger == TriggerKind.UTTERANCE:
                self._tuner.on_user_utterance(text)
        self._trace_logger.info("decision", **DecisionTrace(
            ts=time.time(), trigger=trigger.value, intent=intent.value, emotion=emotion.value,
            actions=actions, rendered=rendered, reason=reason, tier=tier.value,
            cooldown_state={"last_open_ts": self._last_open_ts},
        ).to_dict())


    def _try_l2(self, text: str, situation: dict | None) -> tuple[str, bool, bool]:
        hits = self._sensitive_filter.scan(text)
        if hits:
            # §9.3：审计只记录过滤动作（时间/规则编号/命中类别），不存原文。
            self._audit_logger.info(
                "filter_action",
                action="block_l2_route",
                ts=time.time(),
                rules=hits,
                categories=hits,
            )
            return "", False, False
        try:
            reply = self._bridge.generate(text, situation, self._memory)
        except Exception:
            logger.warning("L2 cloud bridge failed, falling back to L1", exc_info=True)
            return "", False, True
        reply = (reply or "").strip()
        if not reply:
            return "", False, True
        return reply, True, False

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
                intent_clf=None, emotion_clf=None, policy=None, bridge=None,
                tuner=None) -> DecisionHub:
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
        bridge=bridge,
        tuner=tuner,
    )
    bus.subscribe(Topics.AWAKE, hub.on_awake)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
