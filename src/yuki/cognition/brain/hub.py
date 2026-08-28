import math
import threading
import time
import uuid
from collections.abc import Callable

from yuki.cognition.brain.classifier import Emotion, detect_emotion
from yuki.cognition.brain.local.router import GateRoute, RouterDecision, is_crisis
from yuki.cognition.brain.policy import DecisionPolicy, SituationAction, TriggerKind
from yuki.cognition.brain.sink import DecisionSink, TunerSink
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
        emotion,
        actions,
        rendered,
        reason,
        route,
        reply_id,
        cooldown_state,
        situation_provenance=None,
    ) -> None:
        self.ts = ts
        self.trigger = trigger
        self.emotion = emotion
        self.actions = actions
        self.rendered = rendered
        self.reason = reason
        self.route = route
        self.reply_id = reply_id
        self.cooldown_state = cooldown_state
        self.situation_provenance = situation_provenance or {}

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "emotion": self.emotion,
            "actions": [a.name if hasattr(a, "name") else str(a) for a in self.actions],
            "rendered": self.rendered,
            "reason": self.reason,
            "route": self.route,
            "reply_id": self.reply_id,
            "cooldown_state": self.cooldown_state,
            "situation_provenance": self.situation_provenance,
        }


class DecisionHub:
    """Coordinate the binary local/cloud decision and publish completed replies."""

    def __init__(
        self,
        bus,
        *,
        policy=None,
        memory=None,
        registry=None,
        trace_logger=None,
        bridge=None,
        loop=None,
        tuner=None,
        context=None,
        projector=None,
        local_router=None,
        local_composer=None,
        local_enabled: bool = False,
        transition_enabled: bool = True,
        periodic=None,
        periodic_interval: int = 0,
        utterance_observers: list[Callable[[str], None]] | None = None,
    ) -> None:
        self._bus = bus
        self._policy = policy or DecisionPolicy(proactive_cooldown_s=120.0)
        self._memory = memory
        self._registry = registry
        self._trace_logger = trace_logger or get_decision_logger()
        self._bridge = bridge
        self._loop = loop or (getattr(bridge, "loop", None) if bridge is not None else None)
        self._tuner = tuner
        self._sinks: list[DecisionSink] = []
        if tuner is not None:
            self._sinks.append(TunerSink(tuner))
        self._local_router = local_router
        self._local_composer = local_composer
        self._local_enabled = local_enabled
        self._decision_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._pending_input_ts = 0.0
        self._transition_enabled = bool(transition_enabled)
        self._periodic = list(periodic or [])
        self._periodic_interval = max(0, int(periodic_interval or 0))
        self._utterance_count = 0
        self._periodic_running = False
        self._periodic_pending = 0
        self._periodic_lock = threading.Lock()
        self._utterance_observers = list(utterance_observers or [])
        self._context = None
        self._situation_fast = None
        self._situation_deep = None
        self._last_open_ts = None
        self._context_wrapper = context
        self._projector = projector

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
        self._handle(TriggerKind.UTTERANCE, payload.get("text", ""), publish_reply=True)

    def on_user_utterance_probe(self, topic: str, payload: dict) -> None:
        ts = (payload or {}).get("ts")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or not math.isfinite(float(ts))
        ):
            logger.warning("ignoring utterance probe without valid timestamp")
            return
        with self._probe_lock:
            self._pending_input_ts = max(self._pending_input_ts, float(ts))

    def _handle(
        self,
        trigger: TriggerKind,
        text: str,
        situation: dict | None = None,
        *,
        publish_reply: bool,
    ) -> dict:
        with self._decision_lock:
            result = self._handle_locked(
                trigger,
                text,
                situation,
                publish_reply=publish_reply,
            )
        if trigger == TriggerKind.UTTERANCE:
            self._notify_utterance_observers(text)
        return result

    def _notify_utterance_observers(self, text: str) -> None:
        for observer in self._utterance_observers:
            try:
                observer(text)
            except Exception:
                logger.warning("utterance observer failed", exc_info=True)

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
            result = self._handle_utterance(
                text,
                snapshot,
                effective_situation,
                publish_reply=publish_reply,
            )
        elif trigger == TriggerKind.SITUATION:
            result = self._handle_situation(trigger, effective_situation)
        else:
            result = self._result("", False, reason="silent", route="silent")

        rendered = result["rendered"]
        spoke = result["spoke"]
        emotion = result["emotion"]
        emotion_value = emotion.value if hasattr(emotion, "value") else str(emotion)
        reply_ts = time.time()
        reply_id = result.get("reply_id")
        if spoke and reply_id is None:
            reply_id = uuid.uuid4().hex
        if spoke:
            self._last_open_ts = reply_ts
            if publish_reply:
                self._bus.publish(
                    Topics.REPLY,
                    {
                        "text": rendered,
                        "ts": reply_ts,
                        "emotion": emotion_value,
                        "kind": "final",
                        "reply_id": reply_id,
                    },
                )

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
                sink.on_user_utterance(text)
            self._utterance_count += 1
            if self._periodic_interval > 0 and self._utterance_count % self._periodic_interval == 0:
                self._run_periodic()

        self._trace_logger.info(
            "decision",
            **DecisionTrace(
                ts=time.time(),
                trigger=trigger.value,
                emotion=emotion_value,
                actions=result["actions"],
                rendered=rendered,
                reason=result["reason"],
                route=result["route"],
                reply_id=reply_id,
                cooldown_state={"last_open_ts": self._last_open_ts},
                situation_provenance=situation_provenance(effective_situation),
            ).to_dict(),
        )
        return {
            "text": rendered,
            "ts": reply_ts,
            "emotion": emotion_value,
            "spoke": spoke,
            "reason": result["reason"],
        }

    def _run_periodic(self) -> None:
        with self._periodic_lock:
            self._periodic_pending += 1
            if self._periodic_running:
                return
            self._periodic_running = True

        def worker() -> None:
            while True:
                with self._periodic_lock:
                    if self._periodic_pending <= 0:
                        self._periodic_running = False
                        return
                    self._periodic_pending -= 1
                for callback in self._periodic:
                    try:
                        callback()
                    except Exception:
                        logger.warning("periodic callback failed", exc_info=True)

        threading.Thread(target=worker, daemon=True, name="yuki-periodic").start()

    def _handle_utterance(
        self,
        text: str,
        snapshot: ContextSnapshot,
        situation: dict | None,
        *,
        publish_reply: bool,
    ) -> dict:
        if is_crisis(text):
            cloud = self._call_cloud(text, snapshot, crisis=True, publish_reply=publish_reply)
            if cloud["failed"] or cloud["interrupted"] or not cloud["spoke"]:
                rendered, spoke = CRISIS_FALLBACK_REPLY, True
            else:
                rendered, spoke = cloud["rendered"], True
            return self._result(
                rendered,
                spoke,
                reason="crisis",
                route=GateRoute.CLOUD,
                emotion=Emotion.SADNESS,
                reply_id=cloud["reply_id"],
            )

        if not self._local_enabled or self._local_router is None:
            return self._cloud_or_notice(
                text,
                snapshot,
                reason="cloud",
                publish_reply=publish_reply,
            )

        decision = self._local_router.route(text, snapshot=snapshot, situation=situation)
        if decision.route == GateRoute.CLOUD:
            return self._cloud_or_notice(
                text,
                snapshot,
                decision=decision,
                reason="cloud",
                publish_reply=publish_reply,
            )
        return self._dispatch_local(
            text,
            snapshot,
            decision,
            publish_reply=publish_reply,
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

    def _dispatch_local(
        self,
        text: str,
        snapshot: ContextSnapshot,
        decision: RouterDecision,
        *,
        publish_reply: bool,
    ) -> dict:
        if self._local_composer is None:
            return self._cloud_or_notice(
                text,
                snapshot,
                decision=decision,
                reason="chat_local_failed",
                publish_reply=publish_reply,
            )
        try:
            rendered = self._local_composer.generate(text, snapshot=snapshot, memory=self._memory)
        except Exception:
            logger.warning("local reply failed, falling back to cloud", exc_info=True)
            return self._cloud_or_notice(
                text,
                snapshot,
                decision=decision,
                reason="chat_local_failed",
                publish_reply=publish_reply,
            )
        if not rendered:
            return self._cloud_or_notice(
                text,
                snapshot,
                decision=decision,
                reason="chat_local_empty",
                publish_reply=publish_reply,
            )
        return self._result(
            rendered,
            True,
            reason="chat_local",
            route=decision.route,
            emotion=detect_emotion(text),
        )

    def _cloud_or_notice(
        self,
        text: str,
        snapshot: ContextSnapshot,
        *,
        decision: RouterDecision | None = None,
        reason: str,
        publish_reply: bool = True,
    ) -> dict:
        cloud = self._call_cloud(text, snapshot, publish_reply=publish_reply)
        if cloud["interrupted"]:
            return self._result(
                "",
                False,
                reason="interrupted",
                route=GateRoute.CLOUD,
                reply_id=cloud["reply_id"],
            )
        if cloud["failed"] or not cloud["spoke"]:
            return self._result(
                L2_UNAVAILABLE_NOTICE,
                True,
                reason="l2_unavailable_fallback",
                route=GateRoute.CLOUD,
                emotion=Emotion.NEUTRAL,
                reply_id=cloud["reply_id"],
            )
        return self._result(
            cloud["rendered"],
            True,
            reason=reason,
            route=GateRoute.CLOUD,
            emotion=detect_emotion(text),
            reply_id=cloud["reply_id"],
        )

    def _call_cloud(
        self,
        text: str,
        snapshot: ContextSnapshot,
        *,
        crisis: bool = False,
        publish_reply: bool,
    ) -> dict:
        if self._loop is not None:
            return self._run_cloud_loop(
                text,
                snapshot,
                crisis=crisis,
                publish_reply=publish_reply,
            )
        rendered, spoke, failed = self._try_cloud(text, snapshot)
        return {
            "rendered": rendered,
            "spoke": spoke,
            "failed": failed,
            "interrupted": False,
            "reply_id": uuid.uuid4().hex,
        }

    def _run_cloud_loop(
        self,
        text: str,
        snapshot: ContextSnapshot,
        *,
        crisis: bool = False,
        publish_reply: bool,
    ) -> dict:
        reply_id = uuid.uuid4().hex
        started = time.time()
        transition_sent = False

        def on_transition(transition: str) -> None:
            nonlocal transition_sent
            transition_sent = True
            self._bus.publish(
                Topics.REPLY,
                {
                    "text": transition,
                    "ts": time.time(),
                    "emotion": "neutral",
                    "kind": "transition",
                    "reply_id": reply_id,
                },
            )

        def interrupt_check() -> bool:
            with self._probe_lock:
                return self._pending_input_ts > started

        try:
            result = self._loop.run(
                text,
                snapshot,
                self._memory,
                crisis=crisis,
                on_transition=(
                    on_transition if publish_reply and self._transition_enabled else None
                ),
                interrupt_check=interrupt_check if publish_reply else None,
            )
        except Exception:
            logger.warning("agent loop failed", exc_info=True)
            return {
                "rendered": "",
                "spoke": False,
                "failed": True,
                "interrupted": False,
                "reply_id": reply_id,
            }
        if result.get("interrupted"):
            if publish_reply and transition_sent:
                self._bus.publish(
                    Topics.REPLY,
                    {
                        "text": "",
                        "ts": time.time(),
                        "emotion": "neutral",
                        "kind": "cancel",
                        "reply_id": reply_id,
                    },
                )
            return {
                "rendered": "",
                "spoke": False,
                "failed": False,
                "interrupted": True,
                "reply_id": reply_id,
            }
        reply = (result.get("text") or "").strip()
        failed = bool(result.get("failed")) or not reply
        return {
            "rendered": "" if failed else reply,
            "spoke": not failed,
            "failed": failed,
            "interrupted": False,
            "reply_id": reply_id,
        }

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

    def _result(
        self,
        rendered: str,
        spoke: bool,
        *,
        reason: str,
        route,
        emotion: Emotion = Emotion.NEUTRAL,
        actions: list | None = None,
        reply_id: str | None = None,
    ) -> dict:
        return {
            "rendered": rendered,
            "spoke": spoke,
            "reason": reason,
            "route": route.value if hasattr(route, "value") else str(route),
            "emotion": emotion,
            "actions": actions or [],
            "reply_id": reply_id,
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
    periodic=None,
    periodic_interval: int = 0,
    utterance_observers: list[Callable[[str], None]] | None = None,
    local_router=None,
    local_composer=None,
    register_awake_service: bool = True,
) -> DecisionHub:
    from yuki.cognition.brain.soul import SoulStore
    from yuki.config import Config

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
        local_router=local_router,
        local_composer=local_composer,
        local_enabled=cfg.local_brain.enabled,
        transition_enabled=cfg.agent_loop.transition_enabled,
        periodic=periodic,
        periodic_interval=periodic_interval,
        utterance_observers=utterance_observers,
    )
    if register_awake_service:
        bus.respond(COGNITION_AWAKE_SERVICE, hub.handle_awake_request)
    bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance)
    if cfg.agent_loop.interrupt_enabled:
        bus.subscribe(Topics.USER_UTTERANCE, hub.on_user_utterance_probe)
    bus.subscribe(Topics.SITUATION_UPDATE, hub.on_situation_update)
    return hub
