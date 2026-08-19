from pathlib import Path

import pytest
from pydantic import ValidationError

from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.persona_name == "yuki"
    assert config.bus.base_port == 5555
    assert config.bus.hwm == 1000
    assert config.bus.auth_token == ""
    assert config.bus.max_msg_size == 10 * 1024 * 1024
    assert config.logging.level == "INFO"
    assert config.supervisor.restart_base_delay == 1.0
    assert config.supervisor.restart_max_delay == 60.0
    assert config.supervisor.restart_window == 600
    assert config.supervisor.restart_max_per_window == 5
    assert config.health.timeout_ms == 2000
    assert config.health.heartbeat_interval_s == 5.0


def test_from_env_merges_env_overrides(monkeypatch):
    monkeypatch.setenv("YUKI_BUS_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_BUS_HWM", "500")
    monkeypatch.setenv("YUKI_LOGGING_LEVEL", "DEBUG")
    monkeypatch.setenv("YUKI_PERSONA_NAME", "aki")
    config = Config.load(None)
    assert config.bus.base_port == 7000
    assert config.bus.hwm == 500
    assert config.logging.level == "DEBUG"
    assert config.persona_name == "aki"

def test_bus_auth_and_size_env_overrides(monkeypatch):
    monkeypatch.setenv("YUKI_BUS_AUTH_TOKEN", "secret")
    monkeypatch.setenv("YUKI_BUS_MAX_MSG_SIZE", "2048")
    config = Config.load(None)
    assert config.bus.auth_token == "secret"
    assert config.bus.max_msg_size == 2048


def test_yaml_then_env_merge(tmp_path, monkeypatch):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8")
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(yaml_file)
    assert config.bus.base_port == 8000  # 来自 YAML
    assert config.bus.hwm == 300         # env 覆盖 YAML
    assert config.logging.level == "INFO"  # 默认


def test_validation_rejects_bad_port():
    with pytest.raises(ValidationError):
        Config(bus={"base_port": 99})

def test_validation_rejects_unknown_section():
    with pytest.raises(ValidationError):
        Config(unknown_section={"x": 1})


def test_load_autodiscovers_config_yaml_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(None)
    assert config.bus.base_port == 8000
    assert config.bus.hwm == 300


def test_memory_defaults():
    config = Config()
    assert config.memory.db_path == "data/yuki.db"
    assert config.memory.decay_base == 1.0
    assert config.memory.decay_lambda == 0.1
    assert config.memory.decay_threshold == 0.02
    assert config.memory.short_term_ttl_s == 1800
    assert config.memory.short_term_capacity == 50
    assert config.memory.cleanup_interval_s == 300.0
    assert config.memory.vector_enabled is False
    assert config.memory.embedding_provider == "hashing"
    assert config.memory.embedding_model == "hashing-v1"
    assert config.memory.embedding_dimension == 384
    assert config.memory.vector_candidates == 30
    assert config.memory.lexical_weight == 0.45
    assert config.memory.vector_weight == 0.45
    assert config.memory.confidence_weight == 0.10


def test_memory_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_MEMORY_DB_PATH", "tmp/mem.db")
    monkeypatch.setenv("YUKI_MEMORY_DECAY_LAMBDA", "0.3")
    monkeypatch.setenv("YUKI_MEMORY_VECTOR_ENABLED", "true")
    monkeypatch.setenv("YUKI_MEMORY_EMBEDDING_DIMENSION", "128")
    monkeypatch.setenv("YUKI_MEMORY_VECTOR_CANDIDATES", "100")
    config = Config.load(None)
    assert config.memory.db_path == "tmp/mem.db"
    assert config.memory.decay_lambda == 0.3
    assert config.memory.vector_enabled is True
    assert config.memory.embedding_dimension == 128
    assert config.memory.vector_candidates == 100


def test_text_defaults():
    config = Config()
    assert config.text.enabled is True
    assert config.text.dom_enabled is True
    assert config.text.uia_enabled is True
    assert config.text.ocr_enabled is False
    assert config.text.ttl_s == 2.0
    assert config.text.max_chars == 50000


def test_text_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_TEXT_ENABLED", "false")
    monkeypatch.setenv("YUKI_TEXT_OCR_ENABLED", "true")
    monkeypatch.setenv("YUKI_TEXT_TTL_S", "5.0")
    config = Config.load(None)
    assert config.text.enabled is False
    assert config.text.ocr_enabled is True
    assert config.text.ttl_s == 5.0


def test_brain_defaults():
    config = Config()
    assert config.brain.proactive_cooldown_s == 120.0
    assert config.brain.proactive_enabled is True


def test_brain_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_BRAIN_PROACTIVE_COOLDOWN_S", "60.0")
    monkeypatch.setenv("YUKI_BRAIN_PROACTIVE_ENABLED", "false")
    config = Config.load(None)
    assert config.brain.proactive_cooldown_s == 60.0
    assert config.brain.proactive_enabled is False


def test_cloud_defaults():
    config = Config()
    assert config.cloud.enabled is False
    assert config.cloud.base_url == "https://api.openai.com/v1"
    assert config.cloud.model == "gpt-4o-mini"
    assert config.cloud.api_key_env == "YUKI_CLOUD_API_KEY"
    assert config.cloud.timeout_s == 10.0
    assert config.cloud.max_turns == 3


def test_cloud_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_CLOUD_ENABLED", "true")
    monkeypatch.setenv("YUKI_CLOUD_MODEL", "gpt-5")
    monkeypatch.setenv("YUKI_CLOUD_TIMEOUT_S", "20.0")
    config = Config.load(None)
    assert config.cloud.enabled is True
    assert config.cloud.model == "gpt-5"
    assert config.cloud.timeout_s == 20.0


def test_vlm_deep_defaults():
    config = Config()
    assert config.vlm.deep_interval_s == 300.0
    assert config.vlm.user_bypass_rate_limit is True


def test_vlm_deep_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_VLM_DEEP_INTERVAL_S", "120.0")
    monkeypatch.setenv("YUKI_VLM_USER_BYPASS_RATE_LIMIT", "false")
    config = Config.load(None)
    assert config.vlm.deep_interval_s == 120.0
    assert config.vlm.user_bypass_rate_limit is False


def test_soul_defaults():
    config = Config()
    assert config.soul.path == "data/soul.json"
    assert config.soul.tuner_state_path == "data/tuner_state.json"


def test_soul_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_SOUL_PATH", "tmp/soul.json")
    monkeypatch.setenv("YUKI_SOUL_TUNER_STATE_PATH", "tmp/tuner_state.json")
    config = Config.load(None)
    assert config.soul.path == "tmp/soul.json"
    assert config.soul.tuner_state_path == "tmp/tuner_state.json"


def test_perception_defaults():
    config = Config()
    assert config.perception.dwell_s == 2.0


def test_perception_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_PERCEPTION_DWELL_S", "1.5")
    config = Config.load(None)
    assert config.perception.dwell_s == 1.5


def test_context_defaults():
    config = Config()
    assert config.context.max_turns == 20
    assert config.context.max_tokens == 1500
    assert config.context.verbatim_turns == 4
    assert config.context.snapshot_path == "data/context_snapshot.json"


def test_context_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_CONTEXT_MAX_TURNS", "30")
    monkeypatch.setenv("YUKI_CONTEXT_SNAPSHOT_PATH", "tmp/snap.json")
    config = Config.load(None)
    assert config.context.max_turns == 30
    assert config.context.snapshot_path == "tmp/snap.json"


def test_sedimenter_defaults():
    config = Config()
    assert config.sedimenter.min_signals == 3
    assert config.sedimenter.confidence_threshold == 0.6
    assert config.sedimenter.topic_engagement_threshold == 3


def test_sedimenter_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_SEDIMENTER_MIN_SIGNALS", "5")
    monkeypatch.setenv("YUKI_SEDIMENTER_CONFIDENCE_THRESHOLD", "0.8")
    config = Config.load(None)
    assert config.sedimenter.min_signals == 5
    assert config.sedimenter.confidence_threshold == 0.8


def test_persona_defaults():
    config = Config()
    assert config.persona.max_versions == 50
    assert config.persona.enable_llm_refine is False
    assert config.persona.snapshots_path == "data/persona_snapshots.json"
    assert "yuki" in config.persona.prompt or "{persona}" in config.persona.prompt


def test_persona_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_PERSONA_MAX_VERSIONS", "100")
    monkeypatch.setenv("YUKI_PERSONA_ENABLE_LLM_REFINE", "true")
    config = Config.load(None)
    assert config.persona.max_versions == 100
    assert config.persona.enable_llm_refine is True


def test_example_config_cloud_points_to_deepseek():
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = Config.load(example)
    assert config.cloud.enabled is False
    assert config.cloud.base_url == "https://api.deepseek.com/v1"
    assert config.cloud.model == "deepseek-v4-flash"
    assert config.cloud.api_key_env == "YUKI_CLOUD_API_KEY"


def test_vlm_defaults():
    config = Config()
    assert config.vlm.enabled is True
    assert config.vlm.model == "Qwen/Qwen3-VL-8B-Instruct"
    assert config.vlm.cache_dir == ""


def test_vlm_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_VLM_ENABLED", "false")
    monkeypatch.setenv("YUKI_VLM_MODEL", "Qwen/Qwen3-VL-8B")
    monkeypatch.setenv("YUKI_VLM_CACHE_DIR", "D:/hf")
    config = Config.load(None)
    assert config.vlm.enabled is False
    assert config.vlm.model == "Qwen/Qwen3-VL-8B"
    assert config.vlm.cache_dir == "D:/hf"
