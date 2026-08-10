from yuki.config import Config


def test_defaults():
    config = Config()
    assert config.base_port == 5555
    assert config.log_level == "INFO"
    assert config.persona_name == "yuki"


def test_from_env(monkeypatch):
    monkeypatch.setenv("YUKI_BASE_PORT", "7000")
    monkeypatch.setenv("YUKI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("YUKI_PERSONA_NAME", "aki")
    config = Config.from_env()
    assert config.base_port == 7000
    assert config.log_level == "DEBUG"
    assert config.persona_name == "aki"


def test_from_env_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("YUKI_BASE_PORT", raising=False)
    monkeypatch.delenv("YUKI_LOG_LEVEL", raising=False)
    monkeypatch.delenv("YUKI_PERSONA_NAME", raising=False)
    config = Config.from_env()
    assert config.base_port == 5555
