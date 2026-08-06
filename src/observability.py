"""Small, dependency-free observability helpers for local runs.

Correlation values live in :mod:`contextvars`, which keeps concurrent async tasks
isolated while allowing ordinary synchronous calls to inherit the current run/test/
conversation context.  Callers can add domain fields; this module owns timestamps,
recursive secret scrubbing, and JSONL serialization.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator


_CORRELATION_FIELDS = ("run_id", "test_id", "conversation_id", "turn_id", "operation_id")
_context: dict[str, ContextVar[str | int | None]] = {
    field: ContextVar(f"avis_{field}", default=None) for field in _CORRELATION_FIELDS
}

_SENSITIVE_KEYS = {
    "api_key", "authorization", "billing_zip", "cvc", "cvv", "email", "openai_api_key",
    "security_code", "x-api-key", "x_api_key",
}
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CVV = re.compile(r"(?i)\b(cvv|cvc|security code)\b\D{0,10}\d{3,4}")


def get_log_dir() -> Path:
    """Return the configured log directory, creating it on first use."""
    default = Path(__file__).resolve().parent.parent / "logs"
    path = Path(os.environ.get("LOG_DIR", default))
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_correlation() -> dict[str, str | int]:
    """Return only correlation fields that are set in the current context."""
    return {name: value for name, variable in _context.items()
            if (value := variable.get()) is not None}


@contextmanager
def correlation_context(**fields: str | int | None) -> Iterator[None]:
    """Temporarily bind known correlation fields and restore their prior values."""
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown correlation field(s): {', '.join(sorted(unknown))}")
    tokens: list[tuple[ContextVar, Token]] = []
    try:
        for name, value in fields.items():
            tokens.append((_context[name], _context[name].set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def set_correlation(**fields: str | int | None) -> None:
    """Set correlation fields for a lifecycle managed by a framework hook."""
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown correlation field(s): {', '.join(sorted(unknown))}")
    for name, value in fields.items():
        _context[name].set(value)


def redact(value: Any) -> Any:
    """Recursively redact secrets in mappings, sequences, and free-form strings."""
    if isinstance(value, dict):
        return {
            key: "***" if str(key).lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        value = _CVV.sub(lambda match: f"{match.group(1)} ***", value)
        value = _CARD.sub("***", value)
        return _EMAIL.sub("***", value)
    return value


def jsonl_logger(name: str, filename: str) -> logging.Logger:
    """Create one process-local JSONL logger for ``LOG_DIR/filename``."""
    logger = logging.getLogger(name)
    target = (get_log_dir() / filename).resolve()
    for handler in logger.handlers:
        if getattr(handler, "_avis_jsonl_target", None) == target:
            return logger

    handler = logging.FileHandler(target)
    handler._avis_jsonl_target = target  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_json(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Write one redacted, correlated JSON event."""
    payload = {
        **fields,
        **current_correlation(),
        "event": event,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    logger.info(json.dumps(redact(payload), default=str, sort_keys=True))
