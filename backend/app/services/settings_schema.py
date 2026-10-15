"""Loads settings-schema.json, the source of truth for game settings and server fields."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "resources" / "settings-schema.json"

# Top-level key order of the shipped enshrouded_server.json. New keys are inserted
# at their canonical position so diffs against the game's own file stay readable.
CANONICAL_TOP_LEVEL_ORDER = [
    "name",
    "saveDirectory",
    "logDirectory",
    "ip",
    "queryPort",
    "slotCount",
    "tags",
    "voiceChatMode",
    "enableVoiceChat",
    "enableTextChat",
    "gameSettingsPreset",
    "gameSettings",
    "userGroups",
    "bannedAccounts",
]

# Default roles the server writes on first launch.
DEFAULT_ROLES: list[dict[str, Any]] = [
    {
        "name": "Admin",
        "canKickBan": True,
        "canAccessInventories": True,
        "canEditWorld": True,
        "canEditBase": True,
        "canExtendBase": True,
        "reservedSlots": 0,
    },
    {
        "name": "Friend",
        "canKickBan": False,
        "canAccessInventories": True,
        "canEditWorld": True,
        "canEditBase": True,
        "canExtendBase": False,
        "reservedSlots": 0,
    },
    {
        "name": "Guest",
        "canKickBan": False,
        "canAccessInventories": False,
        "canEditWorld": True,
        "canEditBase": False,
        "canExtendBase": False,
        "reservedSlots": 0,
    },
    {
        "name": "Visitor",
        "canKickBan": False,
        "canAccessInventories": False,
        "canEditWorld": False,
        "canEditBase": False,
        "canExtendBase": False,
        "reservedSlots": 0,
    },
]

ROLE_KEY_ORDER = [
    "name",
    "password",
    "canKickBan",
    "canAccessInventories",
    "canEditWorld",
    "canEditBase",
    "canExtendBase",
    "reservedSlots",
]


@lru_cache
def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def schema_bytes() -> bytes:
    return SCHEMA_PATH.read_bytes()


def game_setting_defaults() -> dict[str, Any]:
    return {s["key"]: s["default"] for s in load_schema()["settings"]}


def server_field_defaults() -> dict[str, Any]:
    return {f["key"]: f["default"] for f in load_schema()["server"]}


def preset_names() -> list[str]:
    return list(load_schema()["presets"])


def preserved_top_level() -> list[str]:
    return list(load_schema()["preserveTopLevel"])
