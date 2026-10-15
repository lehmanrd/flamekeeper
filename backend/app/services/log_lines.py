"""Parses game container log lines into {ts, source, level, text} for the log viewer."""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import redact

_ANSI = re.compile(r"#033\[[0-9;]*m|\x1b\[[0-9;]*m")
# 2026-10-14 19:55:47.419 supervisord: enshrouded-server [Session] 'HostOnline' (up)!
_PROGRAM_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[.,]\d+) supervisord: (?P<source>[\w-]+) ?(?P<text>.*)$"
)
# 2026-10-14 19:55:37,110 INFO spawned: 'enshrouded-server' with pid 271
_SUPERVISOR_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[.,]\d+) (?P<level>[A-Z]+) (?P<text>.*)$"
)
_ERROR = re.compile(r"\b(ERROR|FATAL|CRITICAL|Internal Error|Failed|failed|Error)\b")
_WARN = re.compile(r"\b(WARN|WARNING|Warning)\b")
_PLAYER = re.compile(r"\[server\] (Player '.+' logged in with|Remove Player ')")


def _level(text: str, hint: str | None = None) -> str:
    if hint in ("ERRO", "ERROR", "CRIT", "CRITICAL"):
        return "error"
    if hint in ("WARN", "WARNING"):
        return "warn"
    if _ERROR.search(text):
        return "error"
    if _WARN.search(text):
        return "warn"
    return "info"


def parse_line(raw: str) -> dict[str, Any]:
    line = redact(_ANSI.sub("", raw)).replace("#011", "    ")
    m = _PROGRAM_LINE.match(line)
    if m:
        text = m.group("text")
        return {"ts": m.group("ts"), "source": m.group("source"), "level": _level(text),
                "text": text, "player": bool(_PLAYER.search(text))}
    m = _SUPERVISOR_LINE.match(line)
    if m:
        text = m.group("text")
        return {"ts": m.group("ts"), "source": "supervisord",
                "level": _level(text, m.group("level")), "text": text, "player": False}
    return {"ts": None, "source": "", "level": _level(line), "text": line, "player": False}


def is_noise(parsed: dict[str, Any]) -> bool:
    """Lines that carry nothing for a human reader (empty steamcmd color resets)."""
    return not parsed["text"].strip()
