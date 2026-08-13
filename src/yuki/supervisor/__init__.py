import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from yuki.bus import BusError, BusTimeoutError, BusNode
from yuki.logger import get_logger

logger = get_logger("yuki.supervisor")


@dataclass
class Child:
    name: str
    cmd: list[str]
    proc: "subprocess.Popen"
    restarts: int = 0
    attempts: int = 0
    last_restart: float = 0.0
    restart_times: list[float] = field(default_factory=list)
    healthy_since: float = 0.0


class Supervisor:
    def __init__(
        self,
        cmds: list[tuple[str, list[str]]],
        popen_factory: Callable = subprocess.Popen,
        restart_delay: float = 1.0,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        restart_base_delay: float = 1.0,
        restart_max_delay: float = 60.0,
        restart_window: int = 600,
        restart_max_per_window: int = 5,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._env = env
        self._clock = clock
        self._sleep = sleep
        self.restart_base_delay = restart_base_delay
        self.restart_max_delay = restart_max_delay
        self.restart_window = restart_window
        self.restart_max_per_window = restart_max_per_window
        self._children: list[Child] = [
            Child(name=name, cmd=cmd, proc=self._spawn(cmd)) for name, cmd in cmds
        ]

    def _spawn(self, cmd: list[str]) -> "subprocess.Popen":
        kwargs = {}
        if self._env is not None:
            kwargs["env"] = self._env
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return self._popen(cmd, **kwargs)

    def tick(self, bus: BusNode | None = None, health_timeout_ms: int = 2000) -> list[str]:
        restarted: list[str] = []
        now = self._clock()
        bus_server = next((c for c in self._children if c.name == "bus_server"), None)
        bus_up = bus_server is None or bus_server.proc.poll() is None
        for child in self._children:
            if child.proc.poll() is None:
                # 存活：超过窗口无重启则清 attempts
                if now - child.healthy_since >= self.restart_window:
                    child.attempts = 0
                    child.healthy_since = now
                # 健康探活（bus_server 只靠 poll 判定，不探活；bus_server 未存活时跳过其余探活）
                if bus is not None and child.name != "bus_server":
                    if not bus_up:
                        logger.info("health probes skipped (bus_server not alive)", process=child.name)
                    else:
                        try:
                            bus.request(f"health/{child.name}", {}, timeout_ms=health_timeout_ms)
                        except BusTimeoutError:
                            logger.warning("health probe failed", process=child.name)
                            self._restart(child, now)
                            restarted.append(child.name)
                        except BusError as exc:
                            if str(exc) == "service not found":
                                # 子进程仍在启动/注册：hub 就绪但服务未注册属瞬时状态，不重启
                                logger.info("health probe pending (service not registered)", process=child.name)
                            else:
                                logger.warning("health probe failed", process=child.name)
                                self._restart(child, now)
                                restarted.append(child.name)
                continue
            self._restart(child, now)
            restarted.append(child.name)
        return restarted

    def _stop_proc(self, child: Child, grace: float = 2.0) -> None:
        proc = child.proc
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            except (OSError, AttributeError):
                pass
        remaining = grace
        while proc.poll() is None and remaining > 0:
            step = min(0.05, remaining)
            self._sleep(step)
            remaining -= step
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

    def _restart(self, child: Child, now: float) -> None:
        now_window = now - self.restart_window
        child.restart_times = [t for t in child.restart_times if t >= now_window]
        if len(child.restart_times) >= self.restart_max_per_window:
            logger.critical("giving up on process (too many restarts in window)", process=child.name)
            return
        delay = min(self.restart_base_delay * (2 ** child.attempts), self.restart_max_delay)
        logger.warning("restarting process", process=child.name, attempt=child.attempts, next_delay=delay)
        self._sleep(delay)
        child.restarts += 1
        child.attempts += 1
        child.restart_times.append(now)
        self._stop_proc(child)
        child.proc = self._spawn(child.cmd)
        child.healthy_since = now

    def terminate_children(self, timeout: float = 5.0) -> None:
        for child in self._children:
            self._stop_proc(child)
        deadline = self._clock() + timeout
        for child in self._children:
            if child.proc.poll() is None:
                try:
                    child.proc.wait(timeout=max(0.0, deadline - self._clock()))
                except subprocess.TimeoutExpired:
                    child.proc.kill()
