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
                            situation={"topic": "量子计算"}, last_open_ts=0.0, now=999.0)
    assert [a.name for a in actions] == ["stay_silent"]


def test_situation_proactive_at_exact_cooldown_boundary():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算"},
                            last_open_ts=180.0, now=300.0)
    assert [a.name for a in actions] == ["acknowledge", "ask"]


def test_situation_proactive_when_cooldown_passed():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算"}, last_open_ts=100.0, now=300.0)
    assert [a.name for a in actions] == ["acknowledge", "ask"]


def test_situation_stay_silent_within_cooldown():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "量子计算"}, last_open_ts=200.0, now=300.0)
    assert [a.name for a in actions] == ["stay_silent"]


def test_situation_stay_silent_when_no_topic():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert [a.name for a in policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                                          situation={"topic": ""}, last_open_ts=0.0, now=999.0)] == ["stay_silent"]
    assert [a.name for a in policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                                          situation=None, last_open_ts=0.0, now=999.0)] == ["stay_silent"]


def test_policy_table_injectable():
    policy = DecisionPolicy(120.0, policy_table={Intent.GAME: ["invite_game"]})
    assert [a.name for a in policy.decide(TriggerKind.UTTERANCE, Intent.GAME, Emotion.NEUTRAL, text="猜数字")] == ["invite_game"]


from yuki.cognition.brain.policy import Tier


def test_tier_for_mapping():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert policy.tier_for(Intent.ENTERTAINMENT) == Tier.L2
    assert policy.tier_for(Intent.CREATIVE) == Tier.L2
    assert policy.tier_for(Intent.ROLEPLAY) == Tier.L2
    assert policy.tier_for(Intent.GAME) == Tier.L2
    assert policy.tier_for(Intent.EMOTIONAL) == Tier.L2
    assert policy.tier_for(Intent.SAFETY) == Tier.L1
    assert policy.tier_for(Intent.CHIT_CHAT) == Tier.L1
    assert policy.tier_for(Intent.UNKNOWN) == Tier.L1


def test_set_cooldown_s_changes_gate():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert policy.cooldown_s == 120.0
    # 基线：原冷却 120 在 now-last=150 时会开口
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "x"},
                            last_open_ts=0.0, now=150.0)
    assert [a.name for a in actions] == ["acknowledge", "ask"]
    policy.set_cooldown_s(200.0)
    assert policy.cooldown_s == 200.0
    # 新冷却 200 在 now-last=150 时应静默
    actions = policy.decide(TriggerKind.SITUATION, Intent.UNKNOWN, Emotion.NEUTRAL,
                            situation={"topic": "x"},
                            last_open_ts=0.0, now=150.0)
    assert [a.name for a in actions] == ["stay_silent"]


def test_binding_core_values_filter_utterance_actions_only():
    policy = DecisionPolicy(
        proactive_cooldown_s=120.0,
        binding_core_values=[{
            "id": "cv.test",
            "role": "binding",
            "blocks": ["ask"],
        }],
    )
    actions = policy.decide(
        TriggerKind.UTTERANCE,
        Intent.EMOTIONAL,
        Emotion.SADNESS,
        text="我有点难过",
    )
    assert [a.name for a in actions] == ["empathize", "write_memory"]
    situation_actions = policy.decide(
        TriggerKind.SITUATION,
        Intent.UNKNOWN,
        Emotion.NEUTRAL,
        situation={"topic": "x"},
        last_open_ts=None,
        now=150.0,
    )
    assert [a.name for a in situation_actions] == ["acknowledge", "ask"]
