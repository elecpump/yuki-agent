from dataclasses import dataclass, field


@dataclass(frozen=True)
class Action:
    name: str
    params: dict = field(default_factory=dict)
