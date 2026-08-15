import logging
import sys
from pathlib import Path

import structlog

_configured = False
_configured_level = "INFO"

def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    """配置日志；force=True 允许在进程启动后应用 config.logging.level。"""
    global _configured, _configured_level
    normalized = level.upper()
    if _configured and (not force or _configured_level == normalized):
        return
    logging.basicConfig(
        level=getattr(logging, normalized, logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
        force=force,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True
    _configured_level = normalized


def _configure_logging_once(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)


def get_file_logger(name: str, filename: str, log_dir: Path = Path("logs")):
    configure_logging()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    stdlib_logger = logging.getLogger(name)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.addHandler(handler)
    stdlib_logger.propagate = False
    return structlog.get_logger(name)


_audit_logger = None
_decision_logger = None


def get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = get_file_logger("yuki.audit", "audit.jsonl")
    return _audit_logger


def get_decision_logger():
    global _decision_logger
    if _decision_logger is None:
        _decision_logger = get_file_logger("yuki.decision", "decision.jsonl")
    return _decision_logger


def bind_trace_id(trace_id: str) -> None:
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def unbind_trace_id() -> None:
    structlog.contextvars.unbind_contextvars("trace_id")
