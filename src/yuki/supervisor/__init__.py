import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from yuki.bus import BUS_HEALTH_SERVICE, BusError, BusTimeoutError, BusNode
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
    next_restart_at: float | None = None
    gave_up: bool = False


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
        startup_grace_s: float = 20.0,
        bus_host: str = "yuki",
        bus_recovery_grace_s: float = 20.0,
        async_restarts: bool = False,
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
        self.startup_grace_s = startup_grace_s
        self.bus_host = bus_host
        self.bus_recovery_grace_s = bus_recovery_grace_s
        self._bus_recovered_at: float | None = None
        self._async_restarts = async_restarts
        self._children: list[Child] = [
            Child(
                name=name,
                cmd=cmd,
                proc=self._spawn(cmd),
                healthy_since=self._clock(),
            )
            for name, cmd in cmds
        ]

    def _spawn(self, cmd: list[str]) -> "subprocess.Popen":
        kwargs = {}
        if self._env is not None:
            kwargs["env"] = self._env
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return self._popen(cmd, **kwargs)


    def _run_due_restarts(self, now: float, restarted: list[str]) -> None:
        for child in self._children:
            if child.next_restart_at is None or now < child.next_restart_at:
                continue
            child.next_restart_at = None
            if self._window_capped(child, now):
                self._mark_given_up(child)
                continue
            child.gave_up = False
            child.restarts += 1
            child.attempts += 1
            child.restart_times.append(now)
            child.proc = self._spawn(child.cmd)
            child.healthy_since = now
            restarted.append(child.name)
    def tick(self, bus: BusNode | None = None, health_timeout_ms: int = 2000) -> list[str]:
        """Probe the bus host first, then dependent children in dependency order."""
        return self._tick_with_bus_host(bus, health_timeout_ms)

    def _tick_with_bus_host(
        self,
        bus: BusNode | None,
        health_timeout_ms: int,
    ) -> list[str]:
        restarted: list[str] = []
        now = self._clock()
        if self._async_restarts:
            self._run_due_restarts(now, restarted)

        host = next((child for child in self._children if child.name == self.bus_host), None)
        if host is None:
            raise ValueError(f"bus host child not found: {self.bus_host}")

        restarted_this_tick: set[str] = set()
        for child in self._children:
            if child.proc.poll() is not None:
                self._restart(child, now)
                restarted.append(child.name)
                restarted_this_tick.add(child.name)
        if (
            host.name in restarted_this_tick
            or host.proc.poll() is not None
            or host.next_restart_at is not None
        ):
            self._bus_recovered_at = None
            return self._visible_restarts(restarted)
        if bus is None:
            return self._visible_restarts(restarted)

        try:
            bus_health = bus.request(
                BUS_HEALTH_SERVICE,
                {},
                timeout_ms=health_timeout_ms,
            )
        except BusError:
            if now - host.healthy_since > self.startup_grace_s:
                self._restart(host, now)
                restarted.append(host.name)
            self._bus_recovered_at = None
            return self._visible_restarts(restarted)
        if not isinstance(bus_health, dict) or bus_health.get("healthy") is False:
            self._restart(host, now)
            restarted.append(host.name)
            self._bus_recovered_at = None
            return self._visible_restarts(restarted)

        if self._bus_recovered_at is None:
            self._bus_recovered_at = now

        for child in self._children:
            if (
                child.name in restarted_this_tick
                or child.proc.poll() is not None
                or child.next_restart_at is not None
            ):
                continue
            service = f"health/{child.name}"
            try:
                result = bus.request(service, {}, timeout_ms=health_timeout_ms)
                if not isinstance(result, dict) or result.get("healthy") is False:
                    self._restart(child, now)
                    restarted.append(child.name)
                    if child is host:
                        self._bus_recovered_at = None
                        return self._visible_restarts(restarted)
            except BusTimeoutError:
                if now - child.healthy_since > self.startup_grace_s:
                    self._restart(child, now)
                    restarted.append(child.name)
                    if child is host:
                        self._bus_recovered_at = None
                        return self._visible_restarts(restarted)
            except BusError as exc:
                within_recovery = (
                    str(exc) == "service not found"
                    and now - self._bus_recovered_at <= self.bus_recovery_grace_s
                )
                within_startup = now - child.healthy_since <= self.startup_grace_s
                if not within_recovery and not within_startup:
                    self._restart(child, now)
                    restarted.append(child.name)
                    if child is host:
                        self._bus_recovered_at = None
                        return self._visible_restarts(restarted)
        return self._visible_restarts(restarted)

    def _visible_restarts(self, restarted: list[str]) -> list[str]:
        if self._async_restarts:
            scheduled = {
                child.name
                for child in self._children
                if child.next_restart_at is not None
            }
            restarted = [name for name in restarted if name not in scheduled]
        return list(dict.fromkeys(restarted))

    def _stop_proc(self, child: Child, grace: float = 2.0) -> None:
        proc = child.proc
        if proc.poll() is not None:
            return
        self._send_break(proc)
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

    def _send_break(self, proc: "subprocess.Popen") -> None:
        if os.name != "nt" or proc.poll() is not None:
            return
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError):
            pass

    def send_break_to_children(self) -> None:
        for child in self._children:
            self._send_break(child.proc)


    def _restart_impl(self, child: Child, now: float) -> None:
        now_window = now - self.restart_window
        child.restart_times = [t for t in child.restart_times if t >= now_window]
        if len(child.restart_times) >= self.restart_max_per_window:
            logger.critical(
                "giving up on process (too many restarts in window)",
                process=child.name,
            )
            return
        delay = min(self.restart_base_delay * (2 ** child.attempts), self.restart_max_delay)
        logger.warning(
            "restarting process",
            process=child.name,
            attempt=child.attempts,
            next_delay=delay,
        )
        # 先停旧进程再退避，避免假死进程在退避期间继续提供坏服务。
        self._stop_proc(child)
        self._sleep(delay)
        child.restarts += 1
        child.attempts += 1
        child.restart_times.append(now)
        child.proc = self._spawn(child.cmd)
        child.healthy_since = now
    def _window_capped(self, child: Child, now: float) -> bool:
        now_window = now - self.restart_window
        child.restart_times = [t for t in child.restart_times if t >= now_window]
        return len(child.restart_times) >= self.restart_max_per_window

    def _mark_given_up(self, child: Child) -> None:
        if child.gave_up:
            return
        child.gave_up = True
        logger.critical(
            "giving up on process (too many restarts in window)",
            process=child.name,
        )

    def _restart(self, child: Child, now: float) -> None:
        if not getattr(self, "_async_restarts"):  # sync path
            self._restart_impl(child, now)
            return
        if child.next_restart_at is not None:
            return
        if self._window_capped(child, now):
            self._mark_given_up(child)
            return
        child.gave_up = False
        delay = min(
            self.restart_base_delay * (2 ** child.attempts),
            self.restart_max_delay,
        )
        logger.warning("scheduling restart", process=child.name, next_delay=delay)
        # 先停旧进程再退避，避免假死进程在退避期间继续提供坏服务。
        self._stop_proc(child)
        child.next_restart_at = now + delay

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
