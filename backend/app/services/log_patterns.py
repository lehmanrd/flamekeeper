"""Classifies game log lines using resources/log-patterns.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATTERNS_PATH = Path(__file__).resolve().parent.parent / "resources" / "log-patterns.yaml"


@dataclass
class Failure:
    regex: re.Pattern[str]
    title: str
    hint: str


@dataclass
class LogEvent:
    kind: str  # host_online | server_process_start | server_shutdown | steam_progress |
    # steam_update_start | steam_success | player_joined | player_left | failure
    data: dict[str, Any]


class LogPatterns:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.simple: list[tuple[str, re.Pattern[str]]] = [
            (k, re.compile(v)) for k, v in raw.items() if isinstance(v, str)
        ]
        self.failures = [
            Failure(re.compile(f["pattern"]), f["title"], f["hint"])
            for f in raw.get("failures", [])
        ]

    def classify(self, line: str) -> LogEvent | None:
        for kind, regex in self.simple:
            m = regex.search(line)
            if m:
                data: dict[str, Any] = dict(m.groupdict())
                if kind == "steam_progress":
                    data["pct"] = float(data["pct"])
                return LogEvent(kind, data)
        for f in self.failures:
            m = f.regex.search(line)
            if m:
                groups = m.groupdict()
                return LogEvent("failure", {"title": f.title.format(**groups),
                                            "hint": f.hint.format(**groups)})
        return None

    def failure_hints(self) -> list[dict[str, str]]:
        return [{"pattern": f.regex.pattern, "title": f.title, "hint": f.hint}
                for f in self.failures]


@lru_cache
def load_patterns() -> LogPatterns:
    return LogPatterns(yaml.safe_load(PATTERNS_PATH.read_text()))
