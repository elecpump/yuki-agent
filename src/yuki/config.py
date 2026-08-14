import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class BusConfig(BaseModel):
    base_port: int = Field(5555, ge=1024, le=65535)
    hwm: int = Field(1000, ge=1)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class SupervisorConfig(BaseModel):
    restart_base_delay: float = 1.0
    restart_max_delay: float = 60.0
    restart_window: int = 600
    restart_max_per_window: int = 5


class HealthConfig(BaseModel):
    timeout_ms: int = 2000
    heartbeat_interval_s: float = 5.0


class MemoryConfig(BaseModel):
    db_path: str = "data/yuki.db"
    decay_base: float = Field(1.0, ge=0.0)
    decay_lambda: float = Field(0.1, ge=0.0)
    decay_threshold: float = Field(0.02, ge=0.0)
    short_term_ttl_s: float = Field(1800, ge=1)
    short_term_capacity: int = Field(50, ge=1)


class BrainConfig(BaseModel):
    proactive_cooldown_s: float = Field(120.0, ge=0.0)
    proactive_enabled: bool = True


class Config(BaseModel):
    persona_name: str = "yuki"
    bus: BusConfig = Field(default_factory=BusConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)

    @classmethod
    def load(cls, config_file: str | Path | None = None) -> "Config":
        data: dict = {}
        path = Path(config_file) if config_file else None
        if path is None:
            default = Path("config.yaml")
            if default.exists():
                path = default
        if path is not None and path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                data.update(yaml.safe_load(fh) or {})
        cls._apply_env("persona_name", "PERSONA_NAME", data)
        for section_name, section_cls in (
            ("bus", BusConfig),
            ("logging", LoggingConfig),
            ("supervisor", SupervisorConfig),
            ("health", HealthConfig),
            ("memory", MemoryConfig),
            ("brain", BrainConfig),
        ):
            section = data.setdefault(section_name, {})
            for field_name in section_cls.model_fields:
                cls._apply_env(field_name, f"{section_name.upper()}_{field_name.upper()}", section, section_cls)
        return cls(**data)

    @classmethod
    def _apply_env(cls, field_name: str, env_suffix: str, target: dict, model_cls=None) -> None:
        env_key = f"YUKI_{env_suffix}"
        if env_key not in os.environ:
            return
        raw = os.environ[env_key]
        if model_cls is None:
            annotation = cls.model_fields[field_name].annotation
        else:
            annotation = model_cls.model_fields[field_name].annotation
        if annotation is bool:
            target[field_name] = raw.lower() in ("1", "true", "yes")
            return
        try:
            target[field_name] = annotation(raw)
        except (TypeError, ValueError):
            target[field_name] = raw

    @classmethod
    def from_env(cls) -> "Config":
        return cls.load(None)
