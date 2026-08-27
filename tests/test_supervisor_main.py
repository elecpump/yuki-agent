import sys

from yuki.supervisor.main import build_children_cmds


def test_build_children_cmds_returns_two_processes():
    names = [name for name, _ in build_children_cmds()]
    assert names == ["yuki", "model_worker"]


def test_build_children_cmds_appends_trigger_to_yuki():
    cmds = build_children_cmds(["--trigger-after", "1"])
    by_name = dict(cmds)
    assert by_name["yuki"] == [
        sys.executable,
        "-m",
        "yuki.app",
        "--trigger-after",
        "1",
    ]
    assert by_name["model_worker"] == [sys.executable, "-m", "yuki.model_worker"]
