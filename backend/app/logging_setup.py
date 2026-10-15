"""Structured JSON logging to stdout with password redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime

_PASSWORD_FIELD = re.compile(r'("password"\s*:\s*")([^"]*)(")', re.IGNORECASE)
_PASSWORD_KV = re.compile(r"(password[=:]\s*)(\S+)", re.IGNORECASE)

_secrets_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a secret (e.g. a role password) so it is masked in any log line."""
    if value and len(value) >= 4:
        with _secrets_lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    text = _PASSWORD_FIELD.sub(r"\1***\3", text)
    text = _PASSWORD_KV.sub(r"\1***", text)
    with _secrets_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Access lines (health checks, SSE reconnects) are noise at INFO.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
