"""Global settings editable in the UI, stored in the database.

Each one has an environment-variable default (app.config.Settings); a value saved in the
UI overrides it and is applied to the live Settings object.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import session_scope
from app.models import AppSetting

# key -> (type, validator description, check)
EDITABLE: dict[str, dict[str, Any]] = {
    "update_check_interval_min": {"type": "int", "min": 5, "max": 1440,
                                  "label": "Update check interval (minutes)"},
    "steam_info_source": {"type": "enum", "options": ["steamcmd", "api"],
                          "label": "Steam build source"},
    "steam_api_fallback": {"type": "bool", "label": "Fall back to api.steamcmd.net"},
    "default_image_tag": {"type": "str", "max_len": 128, "label": "Default image tag"},
    "first_query_port": {"type": "int", "min": 1024, "max": 65535,
                         "label": "First query port"},
    "metrics_retention_days": {"type": "int", "min": 1, "max": 90,
                               "label": "Metrics retention (days)"},
}


class SettingsError(ValueError):
    pass


def _check(key: str, value: Any) -> Any:
    spec = EDITABLE.get(key)
    if spec is None:
        raise SettingsError(f"{key} can't be changed here")
    kind = spec["type"]
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or \
                not spec["min"] <= value <= spec["max"]:
            raise SettingsError(f"{spec['label']} must be {spec['min']}–{spec['max']}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{spec['label']} must be true or false")
    elif kind == "enum":
        if value not in spec["options"]:
            raise SettingsError(f"{spec['label']} must be one of {', '.join(spec['options'])}")
    elif kind == "str":
        if not isinstance(value, str) or not value.strip() or len(value) > spec["max_len"]:
            raise SettingsError(f"{spec['label']} is required (up to {spec['max_len']} chars)")
        value = value.strip()
    return value


class AppSettings:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env_defaults = {k: getattr(settings, k) for k in EDITABLE}

    def load(self) -> None:
        """Apply saved overrides (at startup)."""
        with session_scope() as s:
            for row in s.query(AppSetting).all():
                if row.key in EDITABLE:
                    try:
                        setattr(self.settings, row.key, _check(row.key, row.value))
                    except SettingsError:
                        pass

    def view(self) -> dict[str, Any]:
        with session_scope() as s:
            saved = {r.key for r in s.query(AppSetting).all()}
        return {"values": {k: getattr(self.settings, k) for k in EDITABLE},
                "defaults": self.env_defaults,
                "overridden": sorted(saved & EDITABLE.keys()),
                "fields": EDITABLE}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        checked = {k: _check(k, v) for k, v in changes.items()}
        with session_scope() as s:
            for k, v in checked.items():
                row = s.get(AppSetting, k)
                if v == self.env_defaults[k]:
                    if row:
                        s.delete(row)  # back to the default
                elif row:
                    row.value = v
                else:
                    s.add(AppSetting(key=k, value=v))
        for k, v in checked.items():
            setattr(self.settings, k, v)
        return self.view()
