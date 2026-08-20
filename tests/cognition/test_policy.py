from yuki.cognition.brain.policy import DecisionPolicy, TriggerKind


def names(actions):
    return [action.name for action in actions]


def test_non_situation_triggers_stay_silent():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert names(policy.decide(TriggerKind.UTTERANCE)) == ["stay_silent"]
    assert names(policy.decide(TriggerKind.AWAKE)) == ["stay_silent"]


def test_situation_stay_silent_when_disabled():
    policy = DecisionPolicy(120.0, proactive_enabled=False)
    actions = policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "量子计算"},
        last_open_ts=0.0,
        now=999.0,
    )
    assert names(actions) == ["stay_silent"]


def test_situation_proactive_at_exact_cooldown_boundary():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "量子计算"},
        last_open_ts=180.0,
        now=300.0,
    )
    assert names(actions) == ["acknowledge", "ask"]


def test_situation_proactive_when_cooldown_passed():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "量子计算"},
        last_open_ts=100.0,
        now=300.0,
    )
    assert names(actions) == ["acknowledge", "ask"]


def test_situation_stay_silent_within_cooldown():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    actions = policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "量子计算"},
        last_open_ts=200.0,
        now=300.0,
    )
    assert names(actions) == ["stay_silent"]


def test_situation_stay_silent_when_no_topic():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert names(policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": ""},
        last_open_ts=0.0,
        now=999.0,
    )) == ["stay_silent"]
    assert names(policy.decide(
        TriggerKind.SITUATION,
        situation=None,
        last_open_ts=0.0,
        now=999.0,
    )) == ["stay_silent"]


def test_binding_core_values_filter_situation_actions():
    policy = DecisionPolicy(
        proactive_cooldown_s=120.0,
        binding_core_values=[{"role": "binding", "blocks": ["ask"]}],
    )
    actions = policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "x"},
        last_open_ts=None,
        now=150.0,
    )
    assert names(actions) == ["acknowledge"]


def test_set_cooldown_s_changes_gate():
    policy = DecisionPolicy(proactive_cooldown_s=120.0)
    assert names(policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "x"},
        last_open_ts=0.0,
        now=150.0,
    )) == ["acknowledge", "ask"]
    policy.set_cooldown_s(200.0)
    assert policy.cooldown_s == 200.0
    assert names(policy.decide(
        TriggerKind.SITUATION,
        situation={"topic": "x"},
        last_open_ts=0.0,
        now=150.0,
    )) == ["stay_silent"]
