import os
import signal
import sys

from yuki.bus import MessageBus
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


def _send_break_to_children(supervisor: Supervisor) -> None:
    for child in supervisor._children:
        if child.proc.poll() is None and os.name == "nt":
            try:
                os.kill(child.proc.pid, signal.CTRL_BREAK_EVENT)
            except (OSError, AttributeError):
                pass


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

    bus = MessageBus(base_port=config.base_port, role="node", hwm=config.hwm)
    supervisor = Supervisor(
        build_children_cmds(extra),
        env=env,
        restart_base_delay=config.restart_base_delay,
        restart_max_delay=config.restart_max_delay,
        restart_window=config.restart_window,
        restart_max_per_window=config.restart_max_per_window,
    )
    try:
        while not shutdown.shutdown_requested:
            try:
                supervisor.tick(bus=bus, health_timeout_ms=config.health_timeout_ms)
            except OSError as exc:
                print(f"[supervisor] spawn failed: {exc}", flush=True)
            shutdown.wait(timeout=0.5)
    finally:
        _send_break_to_children(supervisor)
        supervisor.terminate_children()
        bus.close()


if __name__ == "__main__":
    main()
