"""Reads Keen's enshrouded_server_readme.txt and compares it with settings-schema.json.

Every game install ships the readme next to the server (game/server/), and Keen updates
it when settings change. FlameKeeper parses it after installs and updates to spot new or
changed settings that the schema doesn't know about yet (docs/game-server-behavior.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.settings_schema import load_schema

README_NAME = "enshrouded_server_readme.txt"
NS_PER_MINUTE = 60_000_000_000

_VERSION = re.compile(r"^Version:\s*(?P<v>[\d.]+)", re.M)
# A gameSettings block: title line, then the "gameSettings": { "key": default, } example.
_SETTING = re.compile(
    r'^(?P<title>[^\n"]+)\n"gameSettings": \{\n"(?P<key>\w+)":\s*(?P<default>[^\n]+?),?\n\}\n'
    r"(?P<body>.*?)(?=\n\n\n|\Z)",
    re.M | re.S,
)
# Short form, used for settings added in 1.0.0.0:
#   Required Flame level to enable placing Flame Altars into the Shroud
#   Default: "flameLevelFogRemoval": 11
#   Options: 0-20
_SHORT_SETTING = re.compile(
    r'^(?P<title>[^\n"]+?)[ \t]*\nDefault:\s*"(?P<key>\w+)":\s*(?P<default>[^\n]+?)[ \t]*\n'
    r"(?P<body>.*?)(?=\n\n|\Z)",
    re.M | re.S,
)
_RANGE = re.compile(r"^\s*(?P<min>[\d.]+)\s*-\s*(?P<max>[\d.]+)\s*$")
_MIN = re.compile(r"^Min value:\s*(?P<v>[\d.]+)\s*(?P<unit>minutes?)?", re.M)
_MAX = re.compile(r"^Max value:\s*(?P<v>[\d.]+)\s*(?P<unit>minutes?)?", re.M)
_OPTIONS = re.compile(r"^Options:\s*(?P<v>.+)$", re.M)
_DEFAULT_DOC = re.compile(
    r"^DEFAULT enshrouded_server\.json[^\n]*\n\s*(?P<json>\{.*\})\s*\Z", re.M | re.S
)
_TAGS = re.compile(r"List of available tags:\n(?P<list>(?:\s*\w+,?\n)+)")


@dataclass
class ReadmeSetting:
    key: str
    title: str
    default: Any
    min: float | None = None
    max: float | None = None
    options: list[str] | None = None


@dataclass
class ServerReadme:
    version: str | None
    settings: dict[str, ReadmeSetting]
    tags: list[str]
    default_document: dict[str, Any] | None
    server_keys: list[str] = field(default_factory=list)


def _number(value: str, unit: str | None) -> float:
    n = float(value)
    return n * NS_PER_MINUTE if unit else n


def _options(text: str) -> list[str]:
    # "Easy (which turns the feature off) / Normal / Hard (...)" and "VeryEasy, Easy, ...".
    text = re.sub(r"\([^)]*\)", "", text)
    parts = re.split(r"\s*[/,]\s*", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _scalar(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().strip('"')


def parse_readme(text: str) -> ServerReadme:
    text = text.replace("\r\n", "\n").replace("“", '"').replace("”", '"')
    version = m.group("v") if (m := _VERSION.search(text)) else None

    settings: dict[str, ReadmeSetting] = {}
    # Short-form entries first: a full entry for the same key further down wins.
    for m in _SHORT_SETTING.finditer(text):
        s = ReadmeSetting(key=m.group("key"), title=m.group("title").strip(),
                          default=_scalar(m.group("default")))
        if (opt := _OPTIONS.search(m.group("body"))) and (r := _RANGE.match(opt.group("v"))):
            s.min, s.max = float(r.group("min")), float(r.group("max"))
        settings[s.key] = s
    for m in _SETTING.finditer(text):
        body = m.group("body")
        s = ReadmeSetting(key=m.group("key"), title=m.group("title").strip(),
                          default=_scalar(m.group("default")))
        if mn := _MIN.search(body):
            s.min = _number(mn.group("v"), mn.group("unit"))
        if mx := _MAX.search(body):
            s.max = _number(mx.group("v"), mx.group("unit"))
        if opt := _OPTIONS.search(body):
            values = _options(opt.group("v"))
            if values != ["true", "false"]:
                s.options = values
        settings[s.key] = s

    tags: list[str] = []
    if m := _TAGS.search(text):
        tags = [t.strip().rstrip(",") for t in m.group("list").split("\n") if t.strip()]

    doc = None
    if m := _DEFAULT_DOC.search(text):
        try:
            doc = json.loads(m.group("json"))
        except json.JSONDecodeError:
            doc = None

    server_keys: list[str] = []
    if "\nSERVER SETTINGS\n" in text:
        section = text.split("\nSERVER SETTINGS\n", 1)[-1].split("\nPERMISSION SETTINGS\n", 1)[0]
        server_keys = re.findall(r"^([a-z][A-Za-z]+)\n", section, re.M)
    return ServerReadme(version, settings, tags, doc, server_keys)


def read_readme(server_dir: Path) -> ServerReadme | None:
    path = server_dir / README_NAME
    try:
        return parse_readme(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return None


@dataclass
class Drift:
    kind: str  # new_setting | missing_setting | range | options | default | tags | top_level
    key: str
    message: str
    readme: Any = None
    schema: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "message": self.message,
                "readme": self.readme, "schema": self.schema}


# Known, intentional differences between the readme and the schema, each with a reason.
# A drift that matches an entry here is not reported.
KNOWN_DIFFERENCES: dict[tuple[str, str], str] = {
    ("top_level", "bans"): "Readme 0.9.0.0 calls the ban list 'bans', but the server writes "
                           "'bannedAccounts'. Both are preserved.",
}


def compare(readme: ServerReadme, schema: dict[str, Any] | None = None) -> list[Drift]:
    """Differences that may need a schema update. Order: new settings first."""
    schema = schema or load_schema()
    ours = {s["key"]: s for s in schema["settings"]}
    out: list[Drift] = []
    for key, r in readme.settings.items():
        s = ours.get(key)
        if s is None:
            out.append(Drift("new_setting", key, f"New game setting '{key}' ({r.title}) is not "
                             "in the schema yet", readme=r.__dict__))
            continue
        if s["type"] == "enum":
            values = [o["value"] for o in s["options"]]
            if r.options is not None and sorted(r.options) != sorted(values):
                out.append(Drift("options", key, f"{s['label']}: options changed",
                                 readme=r.options, schema=values))
        else:
            for bound in ("min", "max"):
                rv, sv = getattr(r, bound), s.get(bound)
                if rv is not None and sv is not None and float(rv) != float(sv):
                    out.append(Drift("range", key, f"{s['label']}: {bound} is {rv:g} in the "
                                     f"readme, {sv:g} in the schema", readme=rv, schema=sv))
    doc_settings = (readme.default_document or {}).get("gameSettings") or {}
    for key in doc_settings.keys() - ours.keys() - readme.settings.keys():
        out.append(Drift("new_setting", key, f"New game setting '{key}' (in the default "
                         "config) is not in the schema yet", readme=doc_settings[key]))
    for key, s in ours.items():
        if key not in readme.settings:
            out.append(Drift("missing_setting", key, f"{s['label']} ({key}) is not in the readme"))
    for key, value in doc_settings.items():
        s = ours.get(key)
        if s is not None and value != s["default"]:
            out.append(Drift("default", key, f"{s['label']}: default is {value} in the readme, "
                             f"{s['default']} in the schema", readme=value, schema=s["default"]))
    tag_def = next((f for f in schema["server"] if f["key"] == "tags"), None)
    if tag_def and readme.tags and sorted(readme.tags) != sorted(tag_def["options"]):
        out.append(Drift("tags", "tags", "The list of server tags changed",
                         readme=readme.tags, schema=tag_def["options"]))
    from app.services.settings_schema import CANONICAL_TOP_LEVEL_ORDER

    top_level = list(readme.server_keys)
    top_level += [k for k in (readme.default_document or {}) if k not in top_level]
    for key in top_level:
        if key not in CANONICAL_TOP_LEVEL_ORDER:
            out.append(Drift("top_level", key, f"Top-level key '{key}' in the readme is "
                             "unknown to FlameKeeper"))
    return [d for d in out if (d.kind, d.key) not in KNOWN_DIFFERENCES]


SEEN_FILE = "readme-seen.json"


def check_install(server_dir: Path, state_dir: Path) -> dict[str, Any] | None:
    """Compare a world's installed readme with the schema; remember what was seen.

    Returns None when the install has no readme yet. `changed` is True the first time a
    readme (by content hash) is seen for this world.
    """
    path = server_dir / README_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    sha = hashlib.sha256(raw).hexdigest()
    readme = parse_readme(raw.decode("utf-8", errors="replace"))
    drift = [d.as_dict() for d in compare(readme)]
    seen_path = state_dir / SEEN_FILE
    try:
        seen = json.loads(seen_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}
    changed = seen.get("sha") != sha
    result = {"version": readme.version, "sha": sha, "drift": drift, "changed": changed,
              "previous_version": seen.get("version"), "settings": len(readme.settings)}
    if changed:
        state_dir.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps({"sha": sha, "version": readme.version}))
    return result
