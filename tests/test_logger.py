import json

import structlog

from yuki import logger as logger_mod
from yuki.logger import (
    audit_log,
    bind_trace_id,
    configure_logging,
    get_audit_logger,
    get_decision_logger,
    get_file_logger,
    get_logger,
    get_situation_logger,
    get_toolcall_logger,
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
    assert hasattr(logger_mod, "audit_log")
    assert hasattr(logger_mod, "get_audit_logger")
    assert hasattr(logger_mod, "get_decision_logger")
    assert hasattr(logger_mod, "get_situation_logger")
    assert hasattr(logger_mod, "get_toolcall_logger")
    assert hasattr(logger_mod, "get_logger")
    assert hasattr(logger_mod, "get_file_logger")
    assert hasattr(logger_mod, "configure_logging")
    assert hasattr(logger_mod, "bind_trace_id")
    assert hasattr(logger_mod, "unbind_trace_id")


def test_module_singletons_write_under_logs_dir():
    # audit/decision logger 惰性创建，可调用（写 logs/ 目录，测试不校验内容）
    assert callable(get_audit_logger().info)
    assert callable(get_decision_logger().info)
    assert callable(get_situation_logger().info)
    assert callable(get_toolcall_logger().info)


def test_audit_log_writes_via_audit_logger(monkeypatch):
    class FakeAudit:
        def __init__(self):
            self.calls = []

        def info(self, event, **fields):
            self.calls.append((event, fields))

    fake = FakeAudit()
    monkeypatch.setattr("yuki.logger.get_audit_logger", lambda: fake)
    audit_log("memory.create", memory_id=1, memory_type="personal")
    assert fake.calls == [("memory.create", {"memory_id": 1, "memory_type": "personal"})]
