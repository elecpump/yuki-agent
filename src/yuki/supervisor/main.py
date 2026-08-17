import os
import sys

from yuki.bus import BusNode
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
    env["YUKI_BUS_BASE_PORT"] = str(config.bus.base_port)
    env["YUKI_BUS_HWM"] = str(config.bus.hwm)
    env["YUKI_BUS_AUTH_TOKEN"] = config.bus.auth_token
    env["YUKI_BUS_MAX_MSG_SIZE"] = str(config.bus.max_msg_size)

    bus = BusNode(
        base_port=config.bus.base_port,
        hwm=config.bus.hwm,
        auth_token=config.bus.auth_token,
        max_msg_size=config.bus.max_msg_size,
    )
    supervisor = Supervisor(
        build_children_cmds(extra),
        env=env,
        restart_base_delay=config.supervisor.restart_base_delay,
        restart_max_delay=config.supervisor.restart_max_delay,
        restart_window=config.supervisor.restart_window,
        async_restarts=True,
        restart_max_per_window=config.supervisor.restart_max_per_window,
    )
    try:
        while not shutdown.shutdown_requested:
            try:
                supervisor.tick(bus=bus, health_timeout_ms=config.health.timeout_ms)
            except OSError as exc:
                print(f"[supervisor] spawn failed: {exc}", flush=True)
            shutdown.wait(timeout=0.5)
    finally:
        supervisor.send_break_to_children()
        supervisor.terminate_children()
        bus.close()


if __name__ == "__main__":
    main()
