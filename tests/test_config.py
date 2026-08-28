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
    assert config.bus.register_interval_s == 10.0
    assert config.logging.level == "INFO"
    assert config.supervisor.restart_base_delay == 1.0
    assert config.supervisor.restart_max_delay == 60.0
    assert config.supervisor.restart_window == 600
    assert config.supervisor.restart_max_per_window == 5
    assert config.supervisor.bus_recovery_grace_s == 20.0
    assert config.health.timeout_ms == 2000
    assert config.health.heartbeat_interval_s == 5.0
    assert config.models.gpu_max_concurrency == 1
    assert config.models.policies["local_chat"].priority == 100
    assert config.models.policies["local_chat"].pinned is True
    assert config.models.policies["stt"].evictable is False
    assert config.models.policies["embedding"].priority == 10
    assert config.runtime_bus.subscriber_queue_size == 256


def test_model_policy_override_merges_with_catalog_defaults():
    config = Config(models={"policies": {"vlm": {"priority": 40}}})

    assert config.models.policies["vlm"].priority == 40
    assert config.models.policies["stt"].priority == 90


def test_unknown_model_policy_is_rejected():
    with pytest.raises(ValidationError, match="unknown model policies"):
        Config(models={"policies": {"unknown": {}}})


def test_tts_defaults_and_language_validation():
    config = Config()
    assert config.tts.enabled is False
    assert config.tts.language == "zh"
    assert config.tts.chunk_size == 1024
    assert not hasattr(config.tts, "sample_rate")
    with pytest.raises(ValidationError):
        Config(tts={"language": "ko"})


def test_tts_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_TTS_ENABLED", "true")
    monkeypatch.setenv("YUKI_TTS_LANGUAGE", "ja")
    monkeypatch.setenv("YUKI_TTS_CHUNK_SIZE", "2048")
    config = Config.from_env()
    assert config.tts.enabled is True
    assert config.tts.language == "ja"
    assert config.tts.chunk_size == 2048


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


def test_plugins_container_accepts_arbitrary_plugin_config():
    config = Config(plugins={"weather": {"api_key": "x", "units": "metric"}})
    assert config.plugins["weather"]["units"] == "metric"


def test_plugins_default_empty():
    assert Config().plugins == {}


def test_unknown_top_level_key_still_rejected():
    with pytest.raises(ValidationError):
        Config(typo_section={"x": 1})


def test_load_autodiscovers_config_yaml_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(None)
    assert config.bus.base_port == 8000
    assert config.bus.hwm == 300


def test_load_merges_local_override(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\nplugins:\n  weather:\n    units: metric\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        "bus:\n  base_port: 9000\nplugins:\n  weather:\n    units: imperial\n  maps:\n    zoom: 3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = Config.load(None)
    assert config.bus.base_port == 9000
    assert config.bus.hwm == 200
    assert config.plugins["weather"]["units"] == "imperial"
    assert config.plugins["maps"]["zoom"] == 3


def test_load_env_overrides_local(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("bus:\n  base_port: 9000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_BASE_PORT", "7000")
    config = Config.load(None)
    assert config.bus.base_port == 7000


def test_load_without_local_file_unchanged(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = Config.load(None)
    assert config.bus.base_port == 8000


def test_load_explicit_file_merges_sibling_local(tmp_path):
    main = tmp_path / "main.yaml"
    local = tmp_path / "main.local.yaml"
    main.write_text("bus:\n  base_port: 8000\n", encoding="utf-8")
    local.write_text("bus:\n  hwm: 300\n", encoding="utf-8")
    config = Config.load(main)
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


def test_local_brain_defaults():
    config = Config()
    assert config.local_brain.model_id == "Qwen/Qwen3-1.7B-FP8"
    assert config.local_brain.fp8_dequantize is True
    assert config.local_brain.local_files_only is False


def test_local_brain_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_LOCAL_BRAIN_FP8_DEQUANTIZE", "true")
    monkeypatch.setenv("YUKI_LOCAL_BRAIN_LOCAL_FILES_ONLY", "true")
    config = Config.load(None)
    assert config.local_brain.fp8_dequantize is True
    assert config.local_brain.local_files_only is True


def test_local_brain_rejects_invalid_device():
    with pytest.raises(ValidationError):
        Config(local_brain={"device": "gpu0"})


def test_cloud_defaults():
    config = Config()
    assert config.cloud.enabled is False
    assert config.cloud.base_url == "https://api.openai.com/v1"
    assert config.cloud.model == "gpt-4o-mini"
    assert config.cloud.api_key_env == "YUKI_CLOUD_API_KEY"
    assert config.cloud.timeout_s == 10.0
    assert config.cloud.max_turns == 3


def test_agent_loop_defaults():
    config = Config()
    assert config.agent_loop.max_steps is None
    assert config.agent_loop.max_duration_s == 15.0
    assert config.agent_loop.transition_enabled is True
    assert config.agent_loop.transition_fallback == "让我看一下……"
    assert config.agent_loop.transition_grace_s == 0.8
    assert config.agent_loop.tool_result_max_chars == 2000
    assert config.agent_loop.compact_threshold_tokens == 0
    assert config.agent_loop.interrupt_enabled is True


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


def test_stt_defaults():
    config = Config()
    assert config.stt.enabled is True
    assert config.stt.model == "iic/SenseVoiceSmall"
    assert config.stt.model_dir == ""
    assert config.stt.device == "auto"
    assert config.stt.language == "auto"
    assert config.stt.use_itn is True
    assert config.stt.warmup is True
    assert config.stt.retry_window_s == 60.0
    assert config.stt.vad.model == "fsmn-vad"
    assert config.stt.vad.vad_interval_ms == 400
    assert config.stt.vad.end_silence_ms == 800
    assert config.stt.vad.max_utterance_s == 10.0


def test_stt_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_STT_ENABLED", "false")
    monkeypatch.setenv("YUKI_STT_MODEL", "hub/sense")
    monkeypatch.setenv("YUKI_STT_MODEL_DIR", "D:/models/sense")
    monkeypatch.setenv("YUKI_STT_DEVICE", "cuda:0")
    monkeypatch.setenv("YUKI_STT_LANGUAGE", "zn")
    monkeypatch.setenv("YUKI_STT_USE_ITN", "false")
    monkeypatch.setenv("YUKI_STT_WARMUP", "false")
    monkeypatch.setenv("YUKI_STT_RETRY_WINDOW_S", "12.5")
    monkeypatch.setenv("YUKI_STT_VAD_MODEL", "fsmn-local")
    monkeypatch.setenv("YUKI_STT_VAD_VAD_INTERVAL_MS", "200")
    monkeypatch.setenv("YUKI_STT_VAD_END_SILENCE_MS", "600")
    monkeypatch.setenv("YUKI_STT_VAD_MAX_UTTERANCE_S", "3.0")
    config = Config.load(None)
    assert config.stt.enabled is False
    assert config.stt.model == "hub/sense"
    assert config.stt.model_dir == "D:/models/sense"
    assert config.stt.device == "cuda:0"
    assert config.stt.language == "zn"
    assert config.stt.use_itn is False
    assert config.stt.warmup is False
    assert config.stt.retry_window_s == 12.5
    assert config.stt.vad.model == "fsmn-local"
    assert config.stt.vad.vad_interval_ms == 200
    assert config.stt.vad.end_silence_ms == 600
    assert config.stt.vad.max_utterance_s == 3.0


def test_stt_rejects_invalid_device():
    with pytest.raises(ValidationError):
        Config(stt={"device": "gpu0"})


def test_model_device_accepts_cuda_index():
    config = Config(local_brain={"device": "cuda:1"}, stt={"device": "cuda:2"})
    assert config.local_brain.device == "cuda:1"
    assert config.stt.device == "cuda:2"


def test_soul_defaults():
    config = Config()
    assert config.soul.path == "data/soul.json"
    assert config.soul.tuner_state_path == "data/tuner_state.json"
    assert config.soul.snapshots_dir == "data/soul_snapshots"
    assert config.soul.max_versions == 50
    assert config.soul.min_snapshot_interval_s == 60.0
    assert config.soul.max_description_chars == 2000
    assert config.soul.reflect_every_utterances == 30
    assert config.soul.reflect_interval_s == 3600.0


def test_soul_allows_single_retained_version():
    assert Config(soul={"max_versions": 1}).soul.max_versions == 1


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


def test_wake_word_defaults():
    config = Config()
    assert config.wake_word.enabled is False
    assert config.wake_word.model_path == ""
    assert config.wake_word.threshold == 0.5
    assert config.wake_word.refractory_s == 2.0
    assert config.wake_word.chunk_ms == 80
    assert config.wake_word.pre_roll_s == 1.2
    assert config.wake_word.listen_timeout_s == 10.0
    assert config.wake_word.listen_window_s == 5.0


def test_wake_word_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_WAKE_WORD_ENABLED", "true")
    monkeypatch.setenv("YUKI_WAKE_WORD_MODEL_PATH", "models/yuki.onnx")
    monkeypatch.setenv("YUKI_WAKE_WORD_THRESHOLD", "0.7")
    monkeypatch.setenv("YUKI_WAKE_WORD_REFRACTORY_S", "3.0")
    monkeypatch.setenv("YUKI_WAKE_WORD_CHUNK_MS", "160")
    monkeypatch.setenv("YUKI_WAKE_WORD_PRE_ROLL_S", "1.5")
    monkeypatch.setenv("YUKI_WAKE_WORD_LISTEN_TIMEOUT_S", "8.0")
    monkeypatch.setenv("YUKI_WAKE_WORD_LISTEN_WINDOW_S", "4.0")
    config = Config.load(None)
    assert config.wake_word.enabled is True
    assert config.wake_word.model_path == "models/yuki.onnx"
    assert config.wake_word.threshold == 0.7
    assert config.wake_word.refractory_s == 3.0
    assert config.wake_word.chunk_ms == 160
    assert config.wake_word.pre_roll_s == 1.5
    assert config.wake_word.listen_timeout_s == 8.0
    assert config.wake_word.listen_window_s == 4.0


def test_gateway_defaults():
    config = Config()
    assert config.gateway.enabled is False
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == 8765
    assert config.gateway.cors_origins == ["tauri://localhost"]
    assert config.gateway.cors_origin_regex == r"^http://localhost:\d+$"
    assert config.gateway.ws_heartbeat_timeout_s == 45.0
    assert config.gateway.cleanup_interval_s == 30.0
    assert config.gateway.chat_task_timeout_s == 60.0
    assert config.gateway.history_dir == "data/recordings"


def test_gateway_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("YUKI_GATEWAY_HOST", "127.0.0.2")
    monkeypatch.setenv("YUKI_GATEWAY_PORT", "8766")
    monkeypatch.setenv("YUKI_GATEWAY_CORS_ORIGIN_REGEX", "^http://localhost:3000$")
    monkeypatch.setenv("YUKI_GATEWAY_WS_HEARTBEAT_TIMEOUT_S", "50.0")
    monkeypatch.setenv("YUKI_GATEWAY_CLEANUP_INTERVAL_S", "35.0")
    monkeypatch.setenv("YUKI_GATEWAY_CHAT_TASK_TIMEOUT_S", "12.0")
    monkeypatch.setenv("YUKI_GATEWAY_HISTORY_DIR", "tmp/history")
    config = Config.load(None)
    assert config.gateway.enabled is True
    assert config.gateway.host == "127.0.0.2"
    assert config.gateway.port == 8766
    assert config.gateway.cors_origin_regex == "^http://localhost:3000$"
    assert config.gateway.ws_heartbeat_timeout_s == 50.0
    assert config.gateway.cleanup_interval_s == 35.0
    assert config.gateway.chat_task_timeout_s == 12.0
    assert config.gateway.history_dir == "tmp/history"


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


def test_persona_defaults():
    config = Config()
    assert config.persona.max_versions == 50
    assert config.persona.enable_llm_refine is False
    assert config.persona.refresh_every_utterances == 30
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
