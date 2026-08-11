import pytest
from pydantic import ValidationError

from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.base_port == 5555
    assert config.log_level == "INFO"
    assert config.persona_name == "yuki"
    assert config.bus_role == "hub"
    assert config.restart_base_delay == 1.0
    assert config.restart_max_delay == 60.0
    assert config.restart_window == 600
    assert config.restart_max_per_window == 5
    assert config.health_timeout_ms == 2000
    assert config.hwm == 1000


def test_from_env_merges_env_overrides(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("YUKI_BUS_ROLE", "node")
    monkeypatch.setenv("YUKI_HWM", "500")
    config = Config.load(None)
    assert config.base_port == 7000
    assert config.log_level == "DEBUG"
    assert config.bus_role == "node"
    assert config.hwm == 500


def test_yaml_then_env_merge(tmp_path, monkeypatch):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("base_port: 8000\nhwm: 200\n", encoding="utf-8")
    monkeypatch.setenv("YUKI_HWM", "300")
    config = Config.load(yaml_file)
    assert config.base_port == 8000  # 来自 YAML
    assert config.hwm == 300         # env 覆盖 YAML
    assert config.log_level == "INFO"  # 默认


def test_env_field_name_mapping():
    # YUKI_BASE_PORT -> base_port
    config = Config.load(None)
    assert config.base_port == 5555


def test_validation_rejects_bad_port():
    with pytest.raises(ValidationError):
        Config(base_port=99)  # 端口必须 >= 1024


def test_from_env_backward_compat(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    config = Config.from_env()
    assert config.base_port == 7000
