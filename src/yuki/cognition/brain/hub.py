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
from yuki.logger import get_decision_logger, get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.brain.hub")

L2_UNAVAILABLE_NOTICE = "（云端暂时不可用，我先用本地模式陪你。）"
COGNITION_AWAKE_SERVICE = "cognition.awake"


def situation_provenance(situation: dict | None) -> dict:
    if not situation:
        return {}
    keys = (
        "situation_id",
        "frame_id",
        "source_id",
        "scroll_band",
        "observation_reason",
        "frame_ts",
    )
    return {key: situation[key] for key in keys if key in situation}


class DecisionTrace:
    def __init__(self, *, ts, trigger, intent, emotion, actions, rendered, reason,
                 tier, cooldown_state, situation_provenance=None) -> None:
        self.ts = ts
        self.trigger = trigger
        self.intent = intent
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.tier = tier
        self.cooldown_state = cooldown_state
        self.situation_provenance = situation_provenance or {}

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
            "situation_provenance": self.situation_provenance,
        }


class DecisionHub:
    """Brain 内核：分类 → tier 路由（L2 云桥 / L1 动作链）→ 执行 → 发布 REPLY + 轨迹。"""

    def __init__(self, bus, *, intent_clf=None, emotion_clf=None, policy=None,
                 memory=None, registry=None, l1=None, executors=None, trace_logger=None,
                 bridge=None, tuner=None,
                 context=None, projector=None, sedimenter=None) -> None:
        self._bus = bus
        self._intent_clf = intent_clf or RuleIntentClassifier()
        self._emotion_clf = emotion_clf or RuleEmotionClassifier()
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._l1 = l1 or L1Engine()
        self._executors = executors if executors is not None else ACTION_EXECUTORS
        self._trace_logger = trace_logger or get_decision_logger()
        self._bridge = bridge
        self._tuner = tuner
        self._decision_lock = threading.Lock()
        self._context = None
        self._situation_fast = None
        self._situation_deep = None
        self._last_open_ts = None
        self._context_wrapper = context
        self._projector = projector
        self._sedimenter = sedimenter

    def on_situation_update(self, topic: str, payload: dict) -> None:
        selected = self._select_situation(payload)
        if self._context_wrapper is not None:
            self._context_wrapper.update_situation(selected)
        self._context = selected
        self._handle(TriggerKind.SITUATION, "", situation=selected, publish_reply=True)

    def handle_awake_request(self, payload: dict) -> dict:
        return self._handle(TriggerKind.AWAKE, "", publish_reply=False)

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        self._handle(TriggerKind.UTTERANCE, text, publish_reply=True)


    def _handle(
        self,
        trigger: TriggerKind,
        text: str,
        situation: dict | None = None,
        *,
        publish_reply: bool,
    ) -> dict:
        # SUB worker 会把不同 topic 的 handler 派发到不同线程；
        # 决策状态（context/last_open_ts）必须串行。
        with self._decision_lock:
            return self._handle_locked(trigger, text, situation, publish_reply=publish_reply)

    def _handle_locked(
        self,
        trigger: TriggerKind,
        text: str,
        situation: dict | None = None,
        *,
        publish_reply: bool,
    ) -> dict:
        snapshot = None
        if self._context_wrapper is not None and self._projector is not None:
            snapshot = self._projector.build(self._context_wrapper)
        effective_situation = situation
        if effective_situation is None:
            effective_situation = (
                getattr(snapshot, "situation", None) if snapshot is not None else self._context)

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
            rendered, spoke, l2_failed = self._try_l2(text, effective_situation, snapshot)
            l2_unavailable = l2_failed
            if spoke:
                reason = "l2"
        if not spoke:
            actions = self._policy.decide(
                trigger, intent, emotion, text=text, situation=effective_situation,
                last_open_ts=self._last_open_ts, now=time.time(),
            )
            rendered, spoke = self._execute(
                actions, intent, emotion, text, effective_situation
            )
            reason = "l1" if spoke else "silent"
            if l2_unavailable:
                # §8.2：云端不可用时明确告知用户正在降级到本地，避免"哑巴"。
                rendered = f"{rendered}{L2_UNAVAILABLE_NOTICE}" if spoke else L2_UNAVAILABLE_NOTICE
                spoke = True
                reason = "l2_unavailable_fallback"
        reply_ts = time.time()
        if spoke:
            self._last_open_ts = reply_ts
            if publish_reply:
                self._bus.publish(Topics.REPLY, {"text": rendered, "ts": reply_ts})

        if self._context_wrapper is not None:
            if trigger == TriggerKind.UTTERANCE:
                self._context_wrapper.add_user(text)
            if spoke:
                self._context_wrapper.add_agent(rendered)

        if self._tuner is not None:
            if trigger == TriggerKind.SITUATION and spoke:
                self._tuner.on_proactive_open()
            if trigger == TriggerKind.UTTERANCE:
                self._tuner.on_user_utterance(text)
        if self._sedimenter is not None and trigger == TriggerKind.UTTERANCE:
            self._sedimenter.on_user_utterance(text, intent)
            topic = (effective_situation or {}).get("topic")
            if topic:
                self._sedimenter.on_engagement(topic)
        self._trace_logger.info("decision", **DecisionTrace(
            ts=time.time(), trigger=trigger.value, intent=intent.value, emotion=emotion.value,
            actions=actions, rendered=rendered, reason=reason, tier=tier.value,
            cooldown_state={"last_open_ts": self._last_open_ts},
            situation_provenance=situation_provenance(effective_situation),
        ).to_dict())
        return {"text": rendered, "ts": reply_ts, "spoke": spoke, "reason": reason}

    def _select_situation(self, payload: dict) -> dict:
        layer = payload.get("layer")
        if layer == "deep":
            self._situation_deep = dict(payload)
        elif layer == "fast":
            self._situation_fast = dict(payload)
        else:
            self._situation_fast = dict(payload)
            self._situation_deep = None
            return dict(payload)

        fast = self._situation_fast
        deep = self._situation_deep
        if (
            deep is not None
            and not deep.get("degraded")
            and (fast is None or deep.get("source_id") == fast.get("source_id"))
        ):
            return dict(deep)
        if fast is not None:
            return dict(fast)
        return dict(payload)


    def _try_l2(self, text: str, situation: dict | None, snapshot=None) -> tuple[str, bool, bool]:
        try:
            reply = self._bridge.generate(text, snapshot, self._memory)
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
                tuner=None, context=None, projector=None, sedimenter=None,
                register_awake_service: bool = True) -> DecisionHub:
    from yuki.config import Config
    from yuki.cognition.brain.soul import SoulStore
    cfg = config or Config.from_env()
    if policy is None:
        soul = SoulStore(
            cfg.soul.path,
            cfg.persona_name,
            default_description=cfg.persona.prompt.format(persona=cfg.persona_name),
            tuner_state_path=cfg.soul.tuner_state_path,
        )
        policy = DecisionPolicy(
            proactive_cooldown_s=cfg.brain.proactive_cooldown_s,
            proactive_enabled=cfg.brain.proactive_enabled,
            binding_core_values=soul.binding_core_values(),
        )
    hub = DecisionHub(
        bus,
        intent_clf=intent_clf,
        emotion_clf=emotion_clf,
        policy=policy,
        memory=memory,
        registry=registry,
        bridge=bridge,
        tuner=tuner,
        context=context,
        projector=projector,
        sedimenter=sedimenter,
    )
    if register_awake_service:
        bus.respond(COGNITION_AWAKE_SERVICE, hub.handle_awake_request)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
