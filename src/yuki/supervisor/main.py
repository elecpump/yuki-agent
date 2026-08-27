import os
import sys

from yuki.bus import BusNode
from yuki.config import Config
from yuki.shutdown import ShutdownManager
from yuki.supervisor import Supervisor

CHILDREN = [
    ("yuki", [sys.executable, "-m", "yuki.app"]),
    ("model_worker", [sys.executable, "-m", "yuki.model_worker"]),
]


def build_children_cmds(yuki_extra: list[str] | None = None) -> list[tuple[str, list[str]]]:
    cmds = []
    for name, base in CHILDREN:
        if name == "yuki" and yuki_extra:
            cmds.append((name, base + yuki_extra))
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
    env["YUKI_BUS_REGISTER_INTERVAL_S"] = str(config.bus.register_interval_s)

    bus = BusNode(
        base_port=config.bus.base_port,
        hwm=config.bus.hwm,
        auth_token=config.bus.auth_token,
        max_msg_size=config.bus.max_msg_size,
        register_interval=config.bus.register_interval_s,
    )
    supervisor = Supervisor(
        build_children_cmds(extra),
        env=env,
        restart_base_delay=config.supervisor.restart_base_delay,
        restart_max_delay=config.supervisor.restart_max_delay,
        restart_window=config.supervisor.restart_window,
        async_restarts=True,
        restart_max_per_window=config.supervisor.restart_max_per_window,
        startup_grace_s=config.supervisor.startup_grace_s,
        bus_host="yuki",
        bus_recovery_grace_s=max(
            config.supervisor.bus_recovery_grace_s,
            2 * config.bus.register_interval_s,
            config.supervisor.startup_grace_s,
        ),
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
