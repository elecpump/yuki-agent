import os
from dataclasses import dataclass


@dataclass
class Config:
    base_port: int = 5555
    log_level: str = "INFO"
    persona_name: str = "yuki"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_port=int(os.environ.get("YUKI_BASE_PORT", "5555")),
            log_level=os.environ.get("YUKI_LOG_LEVEL", "INFO"),
            persona_name=os.environ.get("YUKI_PERSONA_NAME", "yuki"),
        )
