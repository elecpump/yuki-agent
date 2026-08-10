import pytest

from yuki.supervisor import Supervisor


class FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.spawn_count = 0

    def poll(self):
        return self._exit_code


def _fake_factory(procs):
    index = {"n": 0}

    def factory(cmd):
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
        restart_delay=0.0,
    )
    restarted = sup.tick(max_restarts=3)
    assert restarted == ["dead"]
    assert dead.spawn_count == 2  # 初始 1 次 + 重启 1 次
    assert ok.spawn_count == 1


def test_supervisor_gives_up_after_max_restarts():
    dead = FakeProc(exit_code=1)
    sup = Supervisor(
        [("dead", ["dead"])],
        popen_factory=_fake_factory({"dead": dead, "ok": None}),
        restart_delay=0.0,
    )
    with pytest.raises(RuntimeError):
        for _ in range(10):
            sup.tick(max_restarts=3)
