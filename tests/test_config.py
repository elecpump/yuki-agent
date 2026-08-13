import pytest
from pydantic import ValidationError

from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.persona_name == "yuki"
    assert config.bus.base_port == 5555
    assert config.bus.hwm == 1000
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


def test_load_autodiscovers_config_yaml_in_cwd(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "bus:\n  base_port: 8000\n  hwm: 200\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_BUS_HWM", "300")
    config = Config.load(None)
    assert config.bus.base_port == 8000
    assert config.bus.hwm == 300
