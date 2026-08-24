from yuki.cognition.load_gate import LoadGate


def test_enabled_gate_ready():
    gate = LoadGate(enabled=True)
    assert gate.disabled() is False
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_disabled_gate_never_loads():
    gate = LoadGate(enabled=False)
    assert gate.disabled() is True
    assert gate.can_load() is False
    assert gate.error_message() == "model disabled"


def test_failure_blocks_until_window_passes():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    gate.mark_failure()
    assert gate.can_load() is False
    assert gate.error_message() == "model load previously failed"
    now[0] = 9.0
    assert gate.can_load() is False
    now[0] = 10.0
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_success_resets_failure_state():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    gate.mark_failure()
    gate.mark_success()
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_health_reports_degraded():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    assert gate.health()["degraded"] is False
    gate.mark_failure()
    health = gate.health()
    assert health["degraded"] is True
    assert health["retry_after_s"] > 0
