import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Config(BaseModel):
    base_port: int = Field(5555, ge=1024, le=65535)
    log_level: str = "INFO"
    persona_name: str = "yuki"
    bus_role: str = "hub"
    restart_base_delay: float = 1.0
    restart_max_delay: float = 60.0
    restart_window: int = 600
    restart_max_per_window: int = 5
    health_timeout_ms: int = 2000
    hwm: int = Field(1000, ge=1)

    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "Config":
        data: dict = {}
        if config_file and Path(config_file).exists():
            with open(config_file, "r", encoding="utf-8") as fh:
                data.update(yaml.safe_load(fh) or {})
        for field in cls.model_fields:
            env_key = f"YUKI_{field.upper()}"
            if env_key in os.environ:
                data[field] = cls._coerce(field, os.environ[env_key])
        return cls(**data)

    @classmethod
    def _coerce(cls, field: str, raw: str):
        annotation = cls.model_fields[field].annotation
        if annotation is bool:
            return raw.lower() in ("1", "true", "yes")
        try:
            return annotation(raw)
        except (TypeError, ValueError):
            return raw

    @classmethod
    def from_env(cls) -> "Config":
        return cls.load(None)
