import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from yuki.logger import get_logger

logger = get_logger("yuki.supervisor")


@dataclass
class Child:
    name: str
    cmd: list[str]
    proc: "subprocess.Popen"
    restarts: int = 0


class Supervisor:
    def __init__(
        self,
        cmds: list[tuple[str, list[str]]],
        popen_factory: Callable = subprocess.Popen,
        restart_delay: float = 1.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._env = env
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

    def tick(self, max_restarts: int = 3) -> list[str]:
        restarted: list[str] = []
        for child in self._children:
            if child.proc.poll() is not None:
                if child.restarts >= max_restarts:
                    raise RuntimeError(f"{child.name} crashed too many times")
                child.restarts += 1
                time.sleep(self._restart_delay)
                child.proc = self._spawn(child.cmd)
                restarted.append(child.name)
        return restarted

    def terminate_children(self, timeout: float = 5.0) -> None:
        for child in self._children:
            if child.proc.poll() is None:
                child.proc.terminate()
        deadline = time.time() + timeout
        for child in self._children:
            if child.proc.poll() is None:
                try:
                    child.proc.wait(timeout=max(0.0, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    child.proc.kill()
