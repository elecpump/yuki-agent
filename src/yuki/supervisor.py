import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable


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
        popen_factory: Callable[[list[str]], "subprocess.Popen"] = subprocess.Popen,
        restart_delay: float = 1.0,
    ) -> None:
        self._popen = popen_factory
        self._restart_delay = restart_delay
        self._children: list[Child] = [
            Child(name=name, cmd=cmd, proc=self._popen(cmd)) for name, cmd in cmds
        ]

    def tick(self, max_restarts: int = 3) -> list[str]:
        restarted: list[str] = []
        for child in self._children:
            if child.proc.poll() is not None:
                if child.restarts >= max_restarts:
                    raise RuntimeError(f"{child.name} crashed too many times")
                child.restarts += 1
                time.sleep(self._restart_delay)
                child.proc = self._popen(child.cmd)
                restarted.append(child.name)
        return restarted
