import sys

from yuki.supervisor.main import build_children_cmds


def test_build_children_cmds_returns_four_layers():
    names = [name for name, _ in build_children_cmds()]
    assert names == ["bus_server", "cognition", "interaction", "perception"]


def test_build_children_cmds_appends_interaction_extra():
    cmds = build_children_cmds(["--trigger-after", "1"])
    by_name = dict(cmds)
    assert by_name["bus_server"] == [sys.executable, "-m", "yuki.bus_server"]
    assert by_name["cognition"] == [sys.executable, "-m", "yuki.cognition"]
    assert by_name["interaction"] == [
        sys.executable,
        "-m",
        "yuki.interaction",
        "--trigger-after",
        "1",
    ]
    assert by_name["perception"] == [sys.executable, "-m", "yuki.perception"]
