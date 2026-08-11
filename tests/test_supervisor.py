import subprocess

import pytest

from yuki.bus import BusTimeoutError
from yuki.supervisor import Supervisor


class FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.spawn_count = 0

    def poll(self):
        return self._exit_code


def _fake_factory(procs):
    index = {"n": 0}

    def factory(cmd, env=None, creationflags=None):
        if cmd[0] == "dead":
            p = procs["dead"]
            p.spawn_count += 1
            return p
        if cmd[0] == "ok":
            p = procs["ok"]
            p.spawn_count += 1
            return p
        raise AssertionError(cmd)

    return factory


def test_supervisor_restarts_dead_process():
    dead = FakeProc(exit_code=1)
    ok = FakeProc(exit_code=None)
    sup = Supervisor(
        [("dead", ["dead"]), ("ok", ["ok"])],
        popen_factory=_fake_factory({"dead": dead, "ok": ok}),
        restart_base_delay=0.0,
    )
    restarted = sup.tick()
    assert restarted == ["dead"]
    assert dead.spawn_count == 2  # 初始 1 次 + 重启 1 次
    assert ok.spawn_count == 1


def test_supervisor_gives_up_after_max_restarts():
    dead = FakeProc(exit_code=1)
    clock = {"now": 0.0}
    sup = Supervisor(
        [("dead", ["dead"])],
        popen_factory=_fake_factory({"dead": dead, "ok": None}),
        restart_base_delay=0.0,
        clock=lambda: clock["now"],
        sleep=lambda s: None,
        restart_window=100,
        restart_max_per_window=2,
    )
    for _ in range(10):
        clock["now"] += 1
        sup.tick()
    assert dead.spawn_count == 3  # 初始 + 窗口内 2 次重启后限流停止


class FakeProcWithState:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated += 1
        self._exit_code = 0

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waited += 1
        self._exit_code = 0
        return 0


def test_terminate_children_terminates_alive():
    child = FakeProcWithState(exit_code=None)
    sup = Supervisor(
        [("cognition", ["python", "-m", "yuki.cognition"])],
        popen_factory=lambda cmd, env=None, creationflags=None: child,
        restart_delay=0.0,
    )
    sup.terminate_children(timeout=1.0)
    assert child.terminated >= 1


def test_terminate_children_kills_on_timeout(monkeypatch):
    class HungryProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("cognition", 1)

        def kill(self):
            self.killed = True

    hungry = HungryProc()
    hungry.killed = False
    sup = Supervisor(
        [("cognition", ["python", "-m", "yuki.cognition"])],
        popen_factory=lambda cmd, env=None, creationflags=None: hungry,
        restart_delay=0.0,
    )
    sup.terminate_children(timeout=0.01)
    assert hungry.killed is True


def test_backoff_increases_with_attempts():
    class P:
        def __init__(self):
            self._alive = False

        def poll(self):
            return 1 if not self._alive else None

    procs = {}
    created = []

    def factory(cmd, env=None, creationflags=None):
        p = P()
        procs[cmd[0]] = p
        created.append(cmd[0])
        return p

    clock = {"now": 100.0}

    def fake_clock():
        return clock["now"]

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=1.0,
        env=None,
        clock=fake_clock,
        sleep=lambda s: None,
        restart_window=600,
        restart_max_per_window=5,
    )
    clock["now"] = 101.0
    sup.tick()
    clock["now"] = 102.0
    sup.tick()
    assert created == ["a", "a", "a"]


def test_window_limit_stops_restarting():
    class DeadProc:
        def poll(self):
            return 1

    created = []

    def factory(cmd, env=None, creationflags=None):
        created.append(cmd[0])
        return DeadProc()

    clock = {"now": 0.0}

    def fake_clock():
        return clock["now"]

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=0.0,
        env=None,
        clock=fake_clock,
        sleep=lambda s: None,
        restart_window=100,
        restart_max_per_window=2,
    )
    for _ in range(5):
        clock["now"] += 1
        sup.tick()
    assert created == ["a", "a", "a"]  # 初始 + 2 次重启后窗口限流停止


def test_health_probe_failure_counts_as_restart():
    created = []
    poll_results = {"a": None}

    class Proc:
        def poll(self):
            return poll_results["a"]

    def factory(cmd, env=None, creationflags=None):
        created.append(cmd[0])
        return Proc()

    class ProbeBus:
        def request(self, service, payload, timeout_ms=2000):
            if service == "health/a":
                raise BusTimeoutError("no heartbeat")

    sup = Supervisor(
        [("a", ["a"])],
        popen_factory=factory,
        restart_delay=0.0,
        env=None,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    sup.tick(bus=ProbeBus(), health_timeout_ms=200)
    assert created == ["a", "a"]  # 初始 + 探活失败重启
