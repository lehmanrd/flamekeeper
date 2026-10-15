"""Human-readable differences between two enshrouded_server.json documents.

Used by the revision history, restore preview and import dry run. Passwords are never
included: a changed password shows as {"secret": true} with no values.
"""

from __future__ import annotations

from typing import Any

from app.services.config_validation import server_fields_by_key, settings_by_key
from app.services.settings_schema import ROLE_KEY_ORDER

_MISSING = object()
_ROLE_LABELS = {
    "name": "Name",
    "password": "Password",
    "canKickBan": "Kick / ban",
    "canAccessInventories": "Access inventories",
    "canEditWorld": "Edit world",
    "canEditBase": "Edit base",
    "canExtendBase": "Extend base",
    "reservedSlots": "Reserved slots",
}


def _val(v: Any) -> Any:
    return None if v is _MISSING else v


def _change(section: str, path: list[str | int], label: str, before: Any, after: Any,
            **extra: Any) -> dict[str, Any]:
    return {"section": section, "path": path, "label": label,
            "before": _val(before), "after": _val(after), **extra}


def _role_key(role: dict[str, Any], i: int) -> str:
    name = role.get("name")
    return name if isinstance(name, str) else f"#{i}"


def _roles_diff(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
    old_roles = [r for r in old if isinstance(r, dict)]
    new_roles = [r for r in new if isinstance(r, dict)]
    old_by = {_role_key(r, i): r for i, r in enumerate(old_roles)}
    new_by = {_role_key(r, i): r for i, r in enumerate(new_roles)}
    out: list[dict[str, Any]] = []
    for name, role in new_by.items():
        if name not in old_by:
            out.append(_change("roles", ["userGroups", name], f"Role {name} added", None, None,
                               kind="added"))
            continue
        before = old_by[name]
        keys = [k for k in ROLE_KEY_ORDER if k in before or k in role]
        keys += [k for k in {**before, **role} if k not in keys]
        for key in keys:
            a, b = before.get(key, _MISSING), role.get(key, _MISSING)
            if a == b:
                continue
            label = f"{name}: {_ROLE_LABELS.get(key, key)}"
            if key == "password":
                out.append(_change("roles", ["userGroups", name, key], label, None, None,
                                   secret=True,
                                   kind="opened" if b == "" else "changed"))
            else:
                out.append(_change("roles", ["userGroups", name, key], label, a, b))
    for name in old_by:
        if name not in new_by:
            out.append(_change("roles", ["userGroups", name], f"Role {name} removed", None, None,
                               kind="removed"))
    common_old = [n for n in old_by if n in new_by]
    common_new = [n for n in new_by if n in old_by]
    if common_old != common_new:
        out.append(_change("roles", ["userGroups"], "Role order", common_old, common_new))
    return out


def diff_documents(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """List of changes from `old` to `new`, in file order."""
    settings = settings_by_key()
    server = server_fields_by_key()
    out: list[dict[str, Any]] = []
    keys = list(old) + [k for k in new if k not in old]
    for key in keys:
        a, b = old.get(key, _MISSING), new.get(key, _MISSING)
        if key == "gameSettings":
            a_gs = a if isinstance(a, dict) else {}
            b_gs = b if isinstance(b, dict) else {}
            gs_keys = list(a_gs) + [k for k in b_gs if k not in a_gs]
            for gk in gs_keys:
                ga, gb = a_gs.get(gk, _MISSING), b_gs.get(gk, _MISSING)
                if ga != gb:
                    label = settings[gk]["label"] if gk in settings else gk
                    out.append(_change("gameSettings", ["gameSettings", gk], label, ga, gb,
                                       key=gk))
            continue
        if key == "userGroups":
            out += _roles_diff(a if isinstance(a, list) else [], b if isinstance(b, list) else [])
            continue
        if a == b:
            continue
        if key == "gameSettingsPreset":
            out.append(_change("preset", [key], "Preset", a, b))
        elif key == "password":
            out.append(_change("legacy", [key], "Legacy password", None, None, secret=True))
        elif key in server:
            out.append(_change("server", [key], server[key]["label"], a, b, key=key))
        else:
            out.append(_change("other", [key], key, a, b))
    return out
