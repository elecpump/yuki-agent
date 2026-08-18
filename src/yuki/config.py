import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class BusConfig(BaseModel):
    base_port: int = Field(5555, ge=1024, le=65533)  # base_port+2 为 ROUTER 端口
    hwm: int = Field(1000, ge=1)
    auth_token: str = ""
    max_msg_size: int = Field(10 * 1024 * 1024, ge=1024)


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
    cleanup_interval_s: float = Field(300.0, ge=10.0)


class TextConfig(BaseModel):
    enabled: bool = True
    dom_enabled: bool = True
    uia_enabled: bool = True
    ocr_enabled: bool = False
    ttl_s: float = Field(2.0, ge=0.0)
    max_chars: int = Field(50000, ge=100)
    summary_chars: int = Field(500, ge=50)
    key_point_chars: int = Field(160, ge=20)
    provider_timeout_ms: int = Field(80, ge=1)
    ocr_timeout_ms: int = Field(250, ge=1)


class BrainConfig(BaseModel):
    proactive_cooldown_s: float = Field(120.0, ge=0.0)
    proactive_enabled: bool = True


class CloudConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "YUKI_CLOUD_API_KEY"
    timeout_s: float = Field(10.0, ge=0.1)
    max_turns: int = Field(3, ge=1)


class SoulConfig(BaseModel):
    path: str = "data/soul.json"


class ContextConfig(BaseModel):
    max_turns: int = Field(20, ge=1)
    max_tokens: int = Field(1500, ge=100)
    verbatim_turns: int = Field(4, ge=1)
    snapshot_path: str = "data/context_snapshot.json"


class SedimenterConfig(BaseModel):
    min_signals: int = Field(3, ge=1)
    confidence_threshold: float = Field(0.6, ge=0.0, le=1.0)
    topic_engagement_threshold: int = Field(3, ge=1)


class PersonaConfig(BaseModel):
    prompt: str = (
        "你是{persona},一个温柔的中文语音陪伴 agent。"
        "回复简短自然(1-3 句),贴合陪伴场景。"
        "不替用户操作系统或浏览器。"
        "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
        "可以用工具查询记忆,但不要捏造记忆内容。"
    )
    max_versions: int = Field(50, ge=1)
    enable_llm_refine: bool = False
    snapshots_path: str = "data/persona_snapshots.json"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_name: str = "yuki"
    bus: BusConfig = Field(default_factory=BusConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    soul: SoulConfig = Field(default_factory=SoulConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    sedimenter: SedimenterConfig = Field(default_factory=SedimenterConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)

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
            ("text", TextConfig),
            ("brain", BrainConfig),
            ("cloud", CloudConfig),
            ("soul", SoulConfig),
            ("context", ContextConfig),
            ("sedimenter", SedimenterConfig),
            ("persona", PersonaConfig),
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
