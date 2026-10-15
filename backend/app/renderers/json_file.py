"""Writes enshrouded_server.json directly: read current file → merge managed keys → write.

Never serializes our own model: every key FlameKeeper does not manage (tags, ban lists,
directories, and anything future game versions add) is preserved, in its original order.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app.renderers.base import ConfigRenderer
from app.services.settings_schema import CANONICAL_TOP_LEVEL_ORDER, ROLE_KEY_ORDER

log = logging.getLogger(__name__)


def _insert_in_canonical_order(
    existing: dict[str, Any], key: str, value: Any, order: list[str]
) -> dict[str, Any]:
    """Return a copy of `existing` with `key` inserted after its nearest canonical predecessor."""
    if key not in order:
        return {**existing, key: value}
    predecessors = order[: order.index(key)]
    anchor = None
    for k in existing:
        if k in predecessors:
            anchor = k
    out: dict[str, Any] = {}
    if anchor is None:
        out[key] = value
    for k, v in existing.items():
        out[k] = v
        if k == anchor:
            out[key] = value
    return out


def _merge_dict(current: dict[str, Any], updates: dict[str, Any], order: list[str]) -> dict:
    result = dict(current)
    for key, value in updates.items():
        if key in result:
            result[key] = value
        else:
            result = _insert_in_canonical_order(result, key, value, order)
    return result


def _merge_roles(current: list[Any], roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roles are positional. Unknown keys of an existing role with the same name are kept."""
    by_name = {r.get("name"): r for r in current if isinstance(r, dict)}
    merged = []
    for role in roles:
        base = by_name.get(role.get("name"), {})
        merged.append(_merge_dict(base, role, ROLE_KEY_ORDER))
    return merged


def merge_server_config(
    current: dict[str, Any], managed: dict[str, Any], remove: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Merge managed keys into the current document, preserving everything else."""
    current = {k: v for k, v in current.items() if k not in remove}
    updates = dict(managed)
    if "gameSettings" in updates:
        game_settings = current.get("gameSettings")
        base = game_settings if isinstance(game_settings, dict) else {}
        updates["gameSettings"] = {**base, **updates["gameSettings"]}
    if "userGroups" in updates:
        groups = current.get("userGroups")
        updates["userGroups"] = _merge_roles(groups if isinstance(groups, list) else [],
                                             updates["userGroups"])
    return _merge_dict(current, updates, CANONICAL_TOP_LEVEL_ORDER)


def detect_indent(raw: str) -> str | int:
    for line in raw.splitlines()[1:]:
        if line.startswith("\t"):
            return "\t"
        if line.startswith(" "):
            return len(line) - len(line.lstrip(" "))
    return "\t"


def dumps_config(doc: dict[str, Any], indent: str | int = "\t") -> str:
    return json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"


def read_config(path: Path) -> tuple[dict[str, Any], str]:
    """Return (document, raw text). Missing file → empty document."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    if not raw.strip():
        return {}, raw
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return doc, raw


def atomic_write(path: Path, content: str, *, uid: int | None, gid: int | None) -> None:
    """Write via temp file + rename in the same directory; set ownership before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o644)
        if uid is not None and gid is not None:
            try:
                os.chown(tmp_name, uid, gid)
            except PermissionError:
                log.warning("cannot chown %s to %s:%s (not root?)", path, uid, gid)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class JsonFileRenderer(ConfigRenderer):
    mode = "json"
    requires_recreate = False

    def container_env(self, managed: dict[str, Any]) -> dict[str, str]:
        return {}

    def write(
        self,
        path: Path,
        managed: dict[str, Any],
        *,
        uid: int | None,
        gid: int | None,
        remove: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        current, raw = read_config(path)
        doc = merge_server_config(current, managed, remove)
        atomic_write(path, dumps_config(doc, detect_indent(raw) if raw else "\t"),
                     uid=uid, gid=gid)
        return doc
