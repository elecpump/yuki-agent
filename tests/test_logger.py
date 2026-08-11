import json

import structlog

from yuki import logger as logger_mod
from yuki.logger import (
    audit_logger,
    bind_trace_id,
    configure_logging,
    decision_logger,
    get_file_logger,
    get_logger,
    unbind_trace_id,
)


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO")  # 不应抛异常


def test_get_logger_is_callable():
    log = get_logger("test")
    assert callable(log.info)
    assert callable(log.warning)
    assert callable(log.error)
    assert callable(log.exception)


def test_file_logger_writes_json_line(tmp_path):
    log = get_file_logger("yuki.audit.test", "audit.jsonl", tmp_path)
    log.info("filter_action", rule="SENSITIVE_PASSWORD", category="credentials")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[0])
    assert data["event"] == "filter_action"
    assert data["rule"] == "SENSITIVE_PASSWORD"
    assert data["category"] == "credentials"


def test_file_logger_appends_lines(tmp_path):
    log = get_file_logger("yuki.decision.test", "decision.jsonl", tmp_path)
    log.info("speak_decision", topic="science", speak=True)
    log.info("speak_decision", topic="history", speak=False)
    lines = (tmp_path / "decision.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["topic"] == "history"


def test_trace_id_binding():
    bind_trace_id("trace-abc")
    assert structlog.contextvars.get_contextvars().get("trace_id") == "trace-abc"
    unbind_trace_id()
    assert "trace_id" not in structlog.contextvars.get_contextvars()


def test_logger_module_exports():
    assert hasattr(logger_mod, "audit_logger")
    assert hasattr(logger_mod, "decision_logger")
    assert hasattr(logger_mod, "get_logger")
    assert hasattr(logger_mod, "get_file_logger")
    assert hasattr(logger_mod, "configure_logging")
    assert hasattr(logger_mod, "bind_trace_id")
    assert hasattr(logger_mod, "unbind_trace_id")


def test_module_singletons_write_under_logs_dir():
    # audit_logger / decision_logger 可调用（写 logs/ 目录，测试不校验内容）
    assert callable(audit_logger.info)
    assert callable(decision_logger.info)
