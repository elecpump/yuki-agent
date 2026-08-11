import os
import sys
import time

from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.supervisor import Supervisor

CHILDREN = [
    ("bus_server", [sys.executable, "-m", "yuki.bus_server"]),
    ("cognition", [sys.executable, "-m", "yuki.cognition"]),
    ("interaction", [sys.executable, "-m", "yuki.interaction"]),
    ("perception", [sys.executable, "-m", "yuki.perception"]),
]


def build_children_cmds(interaction_extra: list[str] | None = None) -> list[tuple[str, list[str]]]:
    cmds = []
    for name, base in CHILDREN:
        if name == "interaction" and interaction_extra:
            cmds.append((name, base + interaction_extra))
        else:
            cmds.append((name, base))
    return cmds


def main() -> None:
    config = Config.from_env()
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()

    extra = None
    if "--trigger-after" in sys.argv:
        index = sys.argv.index("--trigger-after")
        extra = ["--trigger-after", sys.argv[index + 1]]

    env = dict(os.environ)
    env["YUKI_BUS_ROLE"] = "node"
    env["YUKI_BASE_PORT"] = str(config.base_port)

    supervisor = Supervisor(build_children_cmds(extra), env=env)
    try:
        while not shutdown.shutdown_requested:
            try:
                supervisor.tick()
            except RuntimeError as exc:
                print(f"[supervisor] {exc}", flush=True)
            shutdown.wait(timeout=0.5)
    finally:
        supervisor.terminate_children()


if __name__ == "__main__":
    main()
