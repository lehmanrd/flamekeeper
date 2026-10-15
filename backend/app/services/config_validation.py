"""Validates edits to enshrouded_server.json against settings-schema.json.

Enums are checked strictly: an unknown enum string stops the server from booting.
Numbers are checked against the schema ranges (the server would clamp them silently).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from app.services.settings_schema import ROLE_KEY_ORDER, load_schema

ROLE_PERMISSIONS = [k for k in ROLE_KEY_ORDER if k.startswith("can")]
MAX_ROLE_NAME = 32
MAX_PASSWORD = 64
MAX_SERVER_NAME = 100


@dataclass
class Problems:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def error(self, loc: list[str | int], msg: str) -> None:
        # One message per field is enough for the UI.
        if not any(e["loc"] == loc for e in self.errors):
            self.errors.append({"loc": loc, "msg": msg})

    def warn(self, loc: list[str | int], msg: str) -> None:
        self.warnings.append({"loc": loc, "msg": msg})

    def extend(self, other: Problems) -> None:
        for e in other.errors:
            self.error(e["loc"], e["msg"])
        self.warnings += other.warnings


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def settings_by_key() -> dict[str, dict[str, Any]]:
    return {s["key"]: s for s in load_schema()["settings"]}


def server_fields_by_key() -> dict[str, dict[str, Any]]:
    return {f["key"]: f for f in load_schema()["server"]}


def validate_setting(setting: dict[str, Any], value: Any) -> str | None:
    """Return an error message, or None if `value` is valid for this setting."""
    kind = setting["type"]
    if kind == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if kind == "enum":
        allowed = [o["value"] if isinstance(o, dict) else o for o in setting["options"]]
        if value not in allowed:
            return f"must be one of {', '.join(map(str, allowed))} (got {value!r})"
        return None
    if kind in ("factor", "int", "duration_ns"):
        if not _is_number(value):
            return "must be a number"
        if kind in ("int", "duration_ns") and float(value) != int(value):
            return "must be a whole number"
        lo, hi = setting.get("min"), setting.get("max")
        if lo is not None and value < lo or hi is not None and value > hi:
            return f"must be between {lo} and {hi} (got {value})"
        return None
    if kind == "string":
        return None if isinstance(value, str) else "must be text"
    if kind == "enum[]":
        if not isinstance(value, list):
            return "must be a list"
        bad = [v for v in value if v not in setting["options"]]
        if bad:
            return f"unknown value(s) {', '.join(map(repr, bad))}"
        if len(set(value)) != len(value):
            return "contains duplicates"
        return None
    return f"unsupported schema type {kind!r}"


def validate_preset(preset: Any) -> Problems:
    p = Problems()
    if preset not in load_schema()["presets"]:
        p.error(["preset"], f"unknown preset {preset!r}")
    return p


def validate_game_settings(values: dict[str, Any], *, partial: bool) -> Problems:
    p = Problems()
    if not isinstance(values, dict):
        p.error(["gameSettings"], "must be an object")
        return p
    schema = settings_by_key()
    for key, value in values.items():
        setting = schema.get(key)
        if setting is None:
            # The server drops keys it doesn't know on its next boot.
            p.error(["gameSettings", key], "unknown setting")
            continue
        msg = validate_setting(setting, value)
        if msg:
            p.error(["gameSettings", key], f"{setting['label']} {msg}")
    if not partial:
        for key in schema.keys() - values.keys():
            p.error(["gameSettings", key], "missing")
    return p


EDITABLE_SERVER_FIELDS = ("name", "slotCount", "voiceChatMode", "enableVoiceChat",
                          "enableTextChat", "tags")


def validate_server(values: dict[str, Any]) -> Problems:
    """Server fields edited through the config (queryPort and ip belong to the container)."""
    p = Problems()
    if not isinstance(values, dict):
        p.error(["server"], "must be an object")
        return p
    fields = server_fields_by_key()
    for key, value in values.items():
        if key not in EDITABLE_SERVER_FIELDS:
            p.error(["server", key], "not editable here")
            continue
        msg = validate_setting(fields[key], value)
        if msg:
            p.error(["server", key], f"{fields[key]['label']} {msg}")
    name = values.get("name")
    if isinstance(name, str):
        if not name.strip():
            p.error(["server", "name"], "Server name is required")
        elif len(name) > MAX_SERVER_NAME:
            p.error(["server", "name"], f"Server name is longer than {MAX_SERVER_NAME} characters")
    return p


def validate_roles(roles: Any, slot_count: int) -> Problems:
    """Unique names, unique non-empty passwords, ≤ 1 open role,
    reserved slots ≤ slot count, at least one role."""
    p = Problems()
    if not isinstance(roles, list):
        p.error(["roles"], "must be a list")
        return p
    if not roles:
        p.error(["roles"], "At least one role is required")
        return p
    names: dict[str, int] = {}
    passwords: dict[str, int] = {}
    open_roles: list[int] = []
    reserved = 0
    for i, role in enumerate(roles):
        loc: list[str | int] = ["roles", i]
        if not isinstance(role, dict):
            p.error(loc, "must be an object")
            continue
        name = role.get("name")
        if not isinstance(name, str) or not name.strip():
            p.error([*loc, "name"], "Role name is required")
        else:
            if len(name) > MAX_ROLE_NAME:
                p.error([*loc, "name"], f"Role name is longer than {MAX_ROLE_NAME} characters")
            key = name.strip().casefold()
            if key in names:
                p.error([*loc, "name"], f"Another role is already named {name!r}")
            names.setdefault(key, i)
        password = role.get("password")
        if not isinstance(password, str):
            p.error([*loc, "password"], "Password must be text (empty for an open role)")
        elif password == "":
            open_roles.append(i)
        else:
            if len(password) > MAX_PASSWORD:
                p.error([*loc, "password"], f"Password is longer than {MAX_PASSWORD} characters")
            if password != password.strip():
                p.warn([*loc, "password"], "Password starts or ends with a space")
            if password in passwords:
                other = roles[passwords[password]].get("name", passwords[password])
                p.error([*loc, "password"],
                        f"Same password as role {other!r}: players join by password, so "
                        "each role needs its own")
            passwords.setdefault(password, i)
        for perm in ROLE_PERMISSIONS:
            if not isinstance(role.get(perm), bool):
                p.error([*loc, perm], "must be true or false")
        slots = role.get("reservedSlots")
        if not isinstance(slots, int) or isinstance(slots, bool) or not 0 <= slots <= 16:
            p.error([*loc, "reservedSlots"], "Reserved slots must be a whole number from 0 to 16")
        else:
            reserved += slots
    if len(open_roles) > 1:
        for i in open_roles[1:]:
            p.error(["roles", i, "password"],
                    "Only one role can have an empty password: the server can't tell "
                    "which open role a player should get")
    elif open_roles:
        name = roles[open_roles[0]].get("name")
        p.warn(["roles", open_roles[0], "password"],
               f"Open: anyone can join as {name} without a password")
    if reserved > slot_count:
        p.error(["roles"], f"Reserved slots add up to {reserved}, more than the "
                           f"{slot_count} max players")
    return p


def validate_bind_ip(value: str) -> str | None:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return "must be an IP address such as 0.0.0.0"
    return None
