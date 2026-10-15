"""Fallback renderer: game settings and roles as SERVER_GS_* / SERVER_ROLE_* env vars.

Unused: the image keeps the JSON file across restarts (docs/game-server-behavior.md).
Kept in case a future image version stops doing so.
Applying a config with this renderer requires recreating the container.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.renderers.base import ConfigRenderer
from app.renderers.json_file import JsonFileRenderer

_ROLE_ENV = {
    "name": "NAME",
    "password": "PASSWORD",
    "canKickBan": "CAN_KICK_BAN",
    "canAccessInventories": "CAN_ACCESS_INVENTORIES",
    "canEditWorld": "CAN_EDIT_WORLD",
    "canEditBase": "CAN_EDIT_BASE",
    "canExtendBase": "CAN_EXTEND_BASE",
    "reservedSlots": "RESERVED_SLOTS",
}


def _snake_upper(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).upper()


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return str(value)


class EnvRenderer(ConfigRenderer):
    mode = "env"
    requires_recreate = True

    def container_env(self, managed: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        if "gameSettingsPreset" in managed:
            env["SERVER_GS_PRESET"] = managed["gameSettingsPreset"]
        for key, value in (managed.get("gameSettings") or {}).items():
            env[f"SERVER_GS_{_snake_upper(key)}"] = _env_value(value)
        for i, role in enumerate(managed.get("userGroups") or []):
            for key, suffix in _ROLE_ENV.items():
                if key in role:
                    env[f"SERVER_ROLE_{i}_{suffix}"] = _env_value(role[key])
        return env

    def write(
        self,
        path: Path,
        managed: dict[str, Any],
        *,
        uid: int | None,
        gid: int | None,
        remove: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        # The image merges env vars into the file on boot but never deletes roles, so
        # the file is still written to keep deletions and unmanaged keys correct.
        return JsonFileRenderer().write(path, managed, uid=uid, gid=gid, remove=remove)
