import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_model_device(value: str) -> str:
    if value in {"auto", "cpu", "cuda"}:
        return value
    if value.startswith("cuda:") and value.removeprefix("cuda:").isdigit():
        return value
    raise ValueError(f"unsupported device: {value!r}")


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
    vector_enabled: bool = False
    embedding_provider: str = "hashing"
    embedding_model: str = "hashing-v1"
    embedding_dimension: int = Field(384, ge=1)
    embedding_cache_dir: str = ""     # sentence-transformers 的 HF 缓存目录（如 .model）；hashing 不用
    embedding_device: str = "auto"    # auto | cpu | cuda:0；sentence-transformers provider
    vector_candidates: int = Field(30, ge=1)
    lexical_weight: float = Field(0.45, ge=0.0)
    vector_weight: float = Field(0.45, ge=0.0)
    confidence_weight: float = Field(0.10, ge=0.0)


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


class LocalBrainConfig(BaseModel):
    enabled: bool = False
    model_id: str = "Qwen/Qwen3-1.7B-FP8"
    cache_dir: str = ""
    device: str = "auto"
    router_threshold: float = Field(0.7, ge=0.0, le=1.0)
    router_prompt_max_tokens: int = Field(1200, ge=100)
    router_timeout_ms: int = Field(150, ge=1)
    local_prompt_max_tokens: int = Field(6000, ge=100)
    reply_max_tokens: int = Field(256, ge=1)
    local_reply_timeout_ms: int = Field(700, ge=1)
    vision_timeout_ms: int = Field(1200, ge=1)
    retry: int = Field(1, ge=0)
    fp8_dequantize: bool = True
    local_files_only: bool = False
    local_tool_allowlist: list[str] = Field(default_factory=list)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        return _validate_model_device(value)


class VlmConfig(BaseModel):
    enabled: bool = True
    model: str = "Qwen/Qwen3-VL-8B-Instruct"
    cache_dir: str = ""
    deep_interval_s: float = Field(300.0, ge=0.0)
    user_bypass_rate_limit: bool = True


class SttVadConfig(BaseModel):
    model: str = "fsmn-vad"
    vad_interval_ms: int = Field(400, ge=50, le=5000)
    end_silence_ms: int = Field(800, ge=100, le=10000)
    max_utterance_s: float = Field(10.0, ge=1.0, le=60.0)


class SttConfig(BaseModel):
    enabled: bool = True
    model: str = "iic/SenseVoiceSmall"
    model_dir: str = ""
    device: str = "auto"
    language: str = "auto"
    use_itn: bool = True
    warmup: bool = True
    retry_window_s: float = Field(60.0, ge=0.0)
    vad: SttVadConfig = Field(default_factory=SttVadConfig)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        return _validate_model_device(value)


class TtsConfig(BaseModel):
    enabled: bool = False
    cfg_path: str = "checkpoints/config.yaml"
    model_dir: str = "checkpoints"
    use_bf16: bool = True
    language: Literal["zh", "en", "ja", "ar", "es"] = "zh"
    reference_audio_path: str = "data/tts/reference_audio/default.wav"
    chunk_size: int = Field(1024, ge=1)
    retry_window_s: float = Field(60.0, ge=0.0)


class CloudConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "YUKI_CLOUD_API_KEY"
    timeout_s: float = Field(10.0, ge=0.1)
    max_turns: int = Field(3, ge=1)


class SoulConfig(BaseModel):
    path: str = "data/soul.json"
    tuner_state_path: str = "data/tuner_state.json"


class PerceptionConfig(BaseModel):
    dwell_s: float = Field(2.0, ge=0.0)


class WakeWordConfig(BaseModel):
    enabled: bool = False
    model_path: str = ""
    threshold: float = Field(0.5, ge=0.0, le=1.0)
    refractory_s: float = Field(2.0, ge=0.0)
    chunk_ms: int = Field(80, ge=20)
    pre_roll_s: float = Field(1.2, ge=0.0)
    listen_timeout_s: float = Field(10.0, ge=0.0)
    listen_window_s: float = Field(5.0, ge=0.0)


class GatewayConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["tauri://localhost"])
    cors_origin_regex: str = r"^http://localhost:\d+$"
    ws_heartbeat_timeout_s: float = Field(45.0, ge=1.0)
    cleanup_interval_s: float = Field(30.0, ge=1.0)
    chat_task_timeout_s: float = Field(60.0, ge=0.1)
    history_dir: str = "data/recordings"


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
    plugins: dict[str, dict] = Field(default_factory=dict)
    bus: BusConfig = Field(default_factory=BusConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    local_brain: LocalBrainConfig = Field(default_factory=LocalBrainConfig)
    vlm: VlmConfig = Field(default_factory=VlmConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    soul: SoulConfig = Field(default_factory=SoulConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
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
        if path is not None:
            local = path.with_name(path.stem + ".local.yaml")
            if local.exists():
                with open(local, "r", encoding="utf-8") as fh:
                    data = _deep_merge(data, yaml.safe_load(fh) or {})
        cls._apply_env("persona_name", "PERSONA_NAME", data)
        for section_name, section_cls in (
            ("bus", BusConfig),
            ("logging", LoggingConfig),
            ("supervisor", SupervisorConfig),
            ("health", HealthConfig),
            ("memory", MemoryConfig),
            ("text", TextConfig),
            ("brain", BrainConfig),
            ("local_brain", LocalBrainConfig),
            ("vlm", VlmConfig),
            ("stt", SttConfig),
            ("tts", TtsConfig),
            ("cloud", CloudConfig),
            ("soul", SoulConfig),
            ("perception", PerceptionConfig),
            ("wake_word", WakeWordConfig),
            ("gateway", GatewayConfig),
            ("context", ContextConfig),
            ("sedimenter", SedimenterConfig),
            ("persona", PersonaConfig),
        ):
            section = data.setdefault(section_name, {})
            cls._apply_model_env(section_name.upper(), section, section_cls)
        return cls(**data)

    @classmethod
    def _apply_model_env(cls, env_prefix: str, target: dict, model_cls: type[BaseModel]) -> None:
        for field_name, field in model_cls.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                nested = target.setdefault(field_name, {})
                cls._apply_model_env(f"{env_prefix}_{field_name.upper()}", nested, annotation)
                continue
            cls._apply_env(field_name, f"{env_prefix}_{field_name.upper()}", target, model_cls)

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


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge dictionaries recursively, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
