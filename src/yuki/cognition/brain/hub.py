import threading
import time

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.brain.local.router import LocalRoute, RouterDecision, is_crisis
from yuki.cognition.brain.policy import DecisionPolicy, SituationAction, TriggerKind
from yuki.cognition.brain.route import DecisionRouter, RouteDispatcher
from yuki.cognition.brain.sink import DecisionSink, SedimenterSink, TunerSink
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.logger import get_decision_logger, get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.brain.hub")

L2_UNAVAILABLE_NOTICE = "（云端暂时不可用，我先用本地模式陪你。）"
CRISIS_FALLBACK_REPLY = (
    "我在。你现在的安全最重要：如果你可能会伤害自己，请立刻联系身边可信任的人，"
    "或拨打当地紧急电话/危机热线。先不要一个人扛着，我们把眼前这一刻撑过去。"
)
COGNITION_AWAKE_SERVICE = "cognition.awake"
COGNITION_CHAT_SERVICE = "cognition.chat"
SOUL_GET_SERVICE = "soul.get"


class _HubRouteDispatcher:
    def __init__(self, route: LocalRoute, handler) -> None:
        self._route = route
        self._handler = handler

    def can_handle(self, decision: RouterDecision) -> bool:
        return decision.route == self._route

    def dispatch(self, text, snapshot, decision) -> dict:
        return self._handler(text, snapshot, decision)


class _HubCloudFallback:
    def __init__(self, hub: "DecisionHub") -> None:
        self._hub = hub

    def can_handle(self, decision: RouterDecision) -> bool:
        return True

    def dispatch(self, text, snapshot, decision) -> dict:
        return self._hub._cloud_or_notice(text, snapshot, decision=decision, reason="cloud")


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
    def __init__(
        self,
        *,
        ts,
        trigger,
        intent,
        emotion,
        actions,
        rendered,
        reason,
        route,
        cooldown_state,
        situation_provenance=None,
    ) -> None:
        self.ts = ts
        self.trigger = trigger
        self.intent = intent
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.route = route
        self.cooldown_state = cooldown_state
        self.situation_provenance = situation_provenance or {}

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "intent": self.intent,
            "emotion": self.emotion,
            "actions": [a.name if hasattr(a, "name") else str(a) for a in self.actions],
            "rendered": self.rendered,
            "reason": self.reason,
            "route": self.route,
            "cooldown_state": self.cooldown_state,
            "situation_provenance": self.situation_provenance,
        }


class DecisionHub:
    """Brain core after local-brain route rewrite."""

    def __init__(
        self,
        bus,
        *,
        policy=None,
        memory=None,
        registry=None,
        trace_logger=None,
        bridge=None,
        tuner=None,
        context=None,
        projector=None,
        sedimenter=None,
        local_router=None,
        local_composer=None,
        vision_screen=None,
        local_enabled: bool = False,
        local_tool_allowlist: list[str] | None = None,
    ) -> None:
        self._bus = bus
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._trace_logger = trace_logger or get_decision_logger()
        self._bridge = bridge
        self._tuner = tuner
        self._sinks: list[DecisionSink] = []
        if tuner is not None:
            self._sinks.append(TunerSink(tuner))
        if sedimenter is not None:
            self._sinks.append(SedimenterSink(sedimenter))
        self._local_router = local_router
        self._local_composer = local_composer
        self._vision_screen = vision_screen
        self._local_enabled = local_enabled
        self._local_tool_allowlist = set(local_tool_allowlist or [])
        self._decision_lock = threading.Lock()
        self._context = None
        self._situation_fast = None
        self._situation_deep = None
        self._last_open_ts = None
        self._context_wrapper = context
        self._projector = projector
        self._sedimenter = sedimenter
        self._route_registry = DecisionRouter()
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.CHAT_LOCAL, self._dispatch_chat_local,
        ))
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.TOOL_LOCAL, self._dispatch_tool_local,
        ))
        self._route_registry.register(_HubRouteDispatcher(
            LocalRoute.VISION, self._dispatch_vision,
        ))
        self._route_fallback = _HubCloudFallback(self)

    def register_route(self, dispatcher: RouteDispatcher) -> None:
        self._route_registry.register(dispatcher)

    def register_sink(self, sink: DecisionSink) -> None:
        self._sinks.append(sink)

    def on_situation_update(self, topic: str, payload: dict) -> None:
        selected = self._select_situation(payload)
        if self._context_wrapper is not None:
            self._context_wrapper.update_situation(selected)
        self._context = selected
        self._handle(TriggerKind.SITUATION, "", situation=selected, publish_reply=True)

    def handle_awake_request(self, payload: dict) -> dict:
        return self._handle(TriggerKind.AWAKE, "", publish_reply=False)

    def handle_chat_request(self, payload: dict) -> dict:
        return self._handle(
            TriggerKind.UTTERANCE,
            str((payload or {}).get("text", "")),
            publish_reply=False,
        )

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
                getattr(snapshot, "situation", None) if snapshot is not None else self._context
            )
        if snapshot is None:
            snapshot = ContextSnapshot(situation=effective_situation)

        if trigger == TriggerKind.UTTERANCE:
            result = self._handle_utterance(text, snapshot, effective_situation)
        elif trigger == TriggerKind.SITUATION:
            result = self._handle_situation(trigger, effective_situation)
        else:
            result = {
                "rendered": "",
                "spoke": False,
                "reason": "silent",
                "route": "silent",
                "intent": Intent.UNKNOWN,
                "emotion": Emotion.NEUTRAL,
                "actions": [],
                "trusted_metadata": False,
            }

        rendered = result["rendered"]
        spoke = result["spoke"]
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

        if trigger == TriggerKind.SITUATION and spoke:
            for sink in self._sinks:
                sink.on_proactive_open()
        if trigger == TriggerKind.UTTERANCE:
            for sink in self._sinks:
                if not bool(getattr(sink, "trusted_only", False)):
                    sink.on_user_utterance(text)

        intent = result["intent"]
        if (
            trigger == TriggerKind.UTTERANCE
            and result.get("trusted_metadata")
            and intent != Intent.UNKNOWN
            and result.get("reason") != "crisis"
        ):
            topic = (effective_situation or {}).get("topic")
            for sink in self._sinks:
                if bool(getattr(sink, "trusted_only", False)):
                    sink.on_user_utterance(text, intent)
                    if topic:
                        sink.on_engagement(topic)

        self._trace_logger.info(
            "decision",
            **DecisionTrace(
                ts=time.time(),
                trigger=trigger.value,
                intent=intent.value,
                emotion=result["emotion"].value,
                actions=result["actions"],
                rendered=rendered,
                reason=result["reason"],
                route=result["route"],
                cooldown_state={"last_open_ts": self._last_open_ts},
                situation_provenance=situation_provenance(effective_situation),
            ).to_dict(),
        )
        return {"text": rendered, "ts": reply_ts, "spoke": spoke, "reason": result["reason"]}

    def _handle_utterance(
        self,
        text: str,
        snapshot: ContextSnapshot,
        situation: dict | None,
    ) -> dict:
        if is_crisis(text):
            rendered, spoke, failed = self._try_cloud(text, snapshot)
            if failed or not spoke:
                rendered, spoke = CRISIS_FALLBACK_REPLY, True
            return self._result(
                rendered,
                spoke,
                reason="crisis",
                route=LocalRoute.CLOUD,
                intent=Intent.SAFETY,
                emotion=Emotion.SADNESS,
            )

        if not self._local_enabled or self._local_router is None:
            return self._cloud_or_notice(text, snapshot, reason="cloud")

        decision = self._local_router.route(text, snapshot=snapshot, situation=situation)
        return self._route_registry.dispatch(
            text,
            snapshot,
            decision,
            fallback=self._route_fallback,
        )

    def _handle_situation(self, trigger: TriggerKind, situation: dict | None) -> dict:
        actions = self._policy.decide(
            trigger,
            situation=situation,
            last_open_ts=self._last_open_ts,
            now=time.time(),
        )
        rendered = self._render_situation_actions(actions, situation)
        return self._result(
            rendered,
            bool(rendered),
            reason="situation" if rendered else "silent",
            route="situation",
            actions=actions,
        )

    def _dispatch_chat_local(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
    ) -> dict:
        if self._local_composer is None:
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="chat_local_failed")
        try:
            rendered = self._local_composer.generate(text, snapshot=snapshot, memory=self._memory)
        except Exception:
            logger.warning("local reply failed, falling back to cloud", exc_info=True)
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="chat_local_failed")
        if not rendered:
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="chat_local_empty")
        return self._result(
            rendered,
            True,
            reason="chat_local",
            route=decision.route,
            intent=decision.intent,
            emotion=decision.emotion,
            trusted_metadata=decision.trusted_metadata,
        )

    def _dispatch_tool_local(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
    ) -> dict:
        if self._registry is None or not self._is_allowed_tool_call(decision.tool_call):
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="tool_local_invalid")
        result = self._registry.dispatch(decision.tool_call)
        if not result.get("ok"):
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="tool_local_failed")
        rendered = self._render_tool_result(result.get("result"))
        return self._result(
            rendered,
            bool(rendered),
            reason="tool_local",
            route=decision.route,
            intent=decision.intent,
            emotion=decision.emotion,
            trusted_metadata=decision.trusted_metadata,
        )

    def _dispatch_vision(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
    ) -> dict:
        if self._vision_screen is None:
            return self._cloud_or_notice(text, snapshot, decision=decision, reason="vision_unavailable")
        context = self._vision_screen.inspect(text)
        if context.get("can_answer"):
            vision_snapshot = self._snapshot_with_situation(snapshot, context)
            return self._dispatch_chat_local(text, vision_snapshot, decision)
        cloud_snapshot = (
            self._snapshot_with_situation(snapshot, context)
            if context and not context.get("degraded")
            else self._snapshot_with_situation(snapshot, None)
        )
        return self._cloud_or_notice(text, cloud_snapshot, decision=decision, reason="vision_cloud")

    def _cloud_or_notice(
        self,
        text: str,
        snapshot: ContextSnapshot,
        *,
        decision: RouterDecision | None = None,
        reason: str,
    ) -> dict:
        rendered, spoke, failed = self._try_cloud(text, snapshot)
        if failed or not spoke:
            rendered, spoke, reason = L2_UNAVAILABLE_NOTICE, True, "l2_unavailable_fallback"
        return self._result(
            rendered,
            spoke,
            reason=reason,
            route=LocalRoute.CLOUD,
            intent=decision.intent if decision else Intent.UNKNOWN,
            emotion=decision.emotion if decision else Emotion.NEUTRAL,
            trusted_metadata=bool(decision and decision.trusted_metadata),
        )

    def _try_cloud(self, text: str, snapshot: ContextSnapshot | None) -> tuple[str, bool, bool]:
        if self._bridge is None:
            return "", False, True
        try:
            reply = self._bridge.generate(text, snapshot, self._memory)
        except Exception:
            logger.warning("cloud bridge failed", exc_info=True)
            return "", False, True
        reply = (reply or "").strip()
        if not reply:
            return "", False, True
        return reply, True, False

    def _is_allowed_tool_call(self, tool_call: dict | None) -> bool:
        if not isinstance(tool_call, dict):
            return False
        name = tool_call.get("name")
        return isinstance(name, str) and name in self._local_tool_allowlist

    def _render_tool_result(self, result) -> str:
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            for key in ("reply", "text", "message"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if result is not None:
            return "好的。"
        return ""

    def _render_situation_actions(
        self,
        actions: list[SituationAction],
        situation: dict | None,
    ) -> str:
        fragments = []
        for action in actions:
            if action.name == "acknowledge":
                topic = (action.params or {}).get("topic") or (situation or {}).get("topic")
                if topic:
                    fragments.append(f"嗯，你正在看{topic}。")
            elif action.name == "ask":
                topic = (situation or {}).get("topic")
                fragments.append(f"关于{topic}，你想聊哪一块？" if topic else "想聊聊吗？")
        return " ".join(fragments)

    def _snapshot_with_situation(
        self,
        snapshot: ContextSnapshot,
        situation: dict | None,
    ) -> ContextSnapshot:
        return ContextSnapshot(
            situation=situation,
            recent_turns=snapshot.recent_turns,
            summaries=snapshot.summaries,
            long_term_memory=snapshot.long_term_memory,
        )

    def _result(
        self,
        rendered: str,
        spoke: bool,
        *,
        reason: str,
        route,
        intent: Intent = Intent.UNKNOWN,
        emotion: Emotion = Emotion.NEUTRAL,
        actions: list | None = None,
        trusted_metadata: bool = False,
    ) -> dict:
        return {
            "rendered": rendered,
            "spoke": spoke,
            "reason": reason,
            "route": route.value if hasattr(route, "value") else str(route),
            "intent": intent,
            "emotion": emotion,
            "actions": actions or [],
            "trusted_metadata": trusted_metadata,
        }

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


def build_brain(
    bus,
    *,
    memory=None,
    registry=None,
    config=None,
    policy=None,
    bridge=None,
    tuner=None,
    context=None,
    projector=None,
    sedimenter=None,
    local_router=None,
    local_composer=None,
    vision_screen=None,
    register_awake_service: bool = True,
) -> DecisionHub:
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
        policy=policy,
        memory=memory,
        registry=registry,
        bridge=bridge,
        tuner=tuner,
        context=context,
        projector=projector,
        sedimenter=sedimenter,
        local_router=local_router,
        local_composer=local_composer,
        vision_screen=vision_screen,
        local_enabled=cfg.local_brain.enabled,
        local_tool_allowlist=cfg.local_brain.local_tool_allowlist,
    )
    if register_awake_service:
        bus.respond(COGNITION_AWAKE_SERVICE, hub.handle_awake_request)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
