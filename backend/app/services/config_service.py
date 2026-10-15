"""Server config (enshrouded_server.json): editing, validation, revisions, import/export.

The file is the source of truth for game settings, server fields and roles.
Every save is a numbered revision: World.config_revision is bumped and a copy of the
written document is kept in config-history/<rev>.json (last 50) for diff and restore.
The API never returns role passwords except through `reveal` and an explicit export.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import session_scope
from app.logging_setup import register_secret
from app.models import World
from app.renderers import ACTIVE_RENDERER, ConfigRenderer
from app.renderers.json_file import atomic_write, dumps_config, read_config
from app.services.config_diff import diff_documents
from app.services.config_validation import (
    EDITABLE_SERVER_FIELDS,
    ROLE_PERMISSIONS,
    Problems,
    validate_game_settings,
    validate_preset,
    validate_roles,
    validate_server,
)
from app.services.settings_schema import (
    DEFAULT_ROLES,
    ROLE_KEY_ORDER,
    game_setting_defaults,
    preset_names,
    server_field_defaults,
)
from app.services.world_paths import WorldPaths

if TYPE_CHECKING:
    from app.services.events import EventLog

log = logging.getLogger(__name__)

HISTORY_KEEP = 50
MASK = "********"
LEGACY_ROLE_NAME = "default"
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_FRIEND = next(r for r in DEFAULT_ROLES if r["name"] == "Friend")


class ConfigError(Exception):
    """A config change was rejected. `errors` carries per-field problems for the UI."""

    def __init__(self, message: str, status: int = 422,
                 errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


def generate_role_password(role_name: str, length: int = 8) -> str:
    """Same shape as the server's own generated passwords: <RoleName><random>."""
    suffix = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
    return f"{role_name}{suffix}"


def default_roles() -> list[dict[str, Any]]:
    # Key order matches the shipped file: name, password, permissions..., reservedSlots.
    return [
        {"name": r["name"], "password": generate_role_password(r["name"]),
         **{k: v for k, v in r.items() if k != "name"}}
        for r in copy.deepcopy(DEFAULT_ROLES)
    ]


def initial_document(
    *,
    name: str,
    query_port: int,
    slot_count: int,
    bind_ip: str = "0.0.0.0",
    preset: str = "Default",
    roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if preset not in preset_names():
        raise ValueError(f"unknown preset {preset!r}")
    server = server_field_defaults()
    return {
        "name": name,
        "saveDirectory": "./savegame",
        "logDirectory": "./logs",
        "ip": bind_ip,
        "queryPort": query_port,
        "slotCount": slot_count,
        "tags": [],
        "voiceChatMode": server["voiceChatMode"],
        "enableVoiceChat": server["enableVoiceChat"],
        "enableTextChat": server["enableTextChat"],
        "gameSettingsPreset": preset,
        "gameSettings": game_setting_defaults(),
        "userGroups": roles if roles is not None else default_roles(),
        "bannedAccounts": [],
    }


def mask_roles(doc: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    for role in out.get("userGroups") or []:
        if isinstance(role, dict) and role.get("password"):
            role["password"] = MASK
    if out.get("password"):
        out["password"] = MASK
    return out


def _roles(doc: dict[str, Any]) -> list[dict[str, Any]]:
    groups = doc.get("userGroups")
    return [r for r in groups if isinstance(r, dict)] if isinstance(groups, list) else []


def _role_view(i: int, role: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"index": i, "name": role.get("name", "")}
    out["has_password"] = bool(role.get("password"))
    for perm in ROLE_PERMISSIONS:
        out[perm] = bool(role.get(perm, False))
    out["reservedSlots"] = role.get("reservedSlots", 0)
    return out


def _effective_game_settings(doc: dict[str, Any]) -> dict[str, Any]:
    """The file's values over schema defaults (the server fills in missing keys too)."""
    current = doc.get("gameSettings") if isinstance(doc.get("gameSettings"), dict) else {}
    defaults = game_setting_defaults()
    return {k: current.get(k, v) for k, v in defaults.items()}


@dataclass
class ConfigUpdate:
    """A partial edit. Omitted sections stay as they are."""

    base_revision: int | None = None
    preset: str | None = None
    game_settings: dict[str, Any] | None = None
    server: dict[str, Any] | None = None
    # Each role: name, permissions, reservedSlots, and either `password` (new value,
    # "" = open) or `keep_password_from` (index of a role in the current file).
    roles: list[dict[str, Any]] | None = None
    remove_legacy_password: bool = False


@dataclass
class SaveResult:
    revision: int
    changes: list[dict[str, Any]]
    warnings: list[dict[str, Any]] = field(default_factory=list)


class ConfigService:
    def __init__(self, worlds_dir: Path, renderer: ConfigRenderer = ACTIVE_RENDERER,
                 events: EventLog | None = None) -> None:
        self.worlds_dir = worlds_dir
        self.renderer = renderer
        self.events = events

    def paths(self, world: World) -> WorldPaths:
        return WorldPaths.for_slug(self.worlds_dir, world.slug)

    # -- reading ------------------------------------------------------------------------

    def read_live(self, world: World) -> dict[str, Any]:
        """The game's enshrouded_server.json as it is on disk."""
        try:
            doc, _ = read_config(self.paths(world).config)
        except (ValueError, json.JSONDecodeError) as e:
            raise ConfigError(f"enshrouded_server.json is not valid JSON: {e}", 409) from e
        _register(doc)
        return doc

    def read(self, world: World) -> dict[str, Any]:
        """The config as FlameKeeper sees it: the live file with any pending revision's
        settings on top. While the server runs, saves are staged in config-history and
        only merged into the live file when the server (re)starts (see apply_pending)."""
        world = _fresh(world)
        live = self.read_live(world)
        pending = self._pending_doc(world)
        if pending is None:
            return live
        return _preview(live, _managed(pending), _removed(live, pending))

    def _pending_doc(self, world: World) -> dict[str, Any] | None:
        if world.config_revision == world.applied_revision:
            return None
        try:
            return self._history_doc(world, world.config_revision)
        except ConfigError:
            return None

    def view(self, world: World) -> dict[str, Any]:
        """The editor's view of the config: everything except passwords."""
        world = _fresh(world)
        doc = self.read(world)
        # What the server last started with; differs from `doc` while a revision is pending.
        live = self.read_live(world) if world.config_revision != world.applied_revision else doc
        server_defaults = server_field_defaults()
        gs = doc.get("gameSettings") if isinstance(doc.get("gameSettings"), dict) else {}
        return {
            "revision": world.config_revision,
            "applied_revision": world.applied_revision,
            "file_exists": self.paths(world).config.exists(),
            "preset": doc.get("gameSettingsPreset", "Default"),
            "gameSettings": _effective_game_settings(doc),
            "applied": {"preset": live.get("gameSettingsPreset", "Default"),
                        "gameSettings": _effective_game_settings(live)},
            "missing_game_settings": sorted(game_setting_defaults().keys() - gs.keys()),
            "unknown_game_settings": sorted(gs.keys() - game_setting_defaults().keys()),
            "server": {
                "name": doc.get("name", world.display_name),
                "slotCount": doc.get("slotCount", server_defaults["slotCount"]),
                "queryPort": doc.get("queryPort", world.query_port),
                "ip": doc.get("ip", server_defaults["ip"]),
                "voiceChatMode": doc.get("voiceChatMode", server_defaults["voiceChatMode"]),
                "enableVoiceChat": doc.get("enableVoiceChat", server_defaults["enableVoiceChat"]),
                "enableTextChat": doc.get("enableTextChat", server_defaults["enableTextChat"]),
                "tags": doc.get("tags", []),
            },
            "roles": [_role_view(i, r) for i, r in enumerate(_roles(doc))],
            "legacy_password": bool(doc.get("password")),
            "warnings": validate_roles(
                _roles(doc), doc.get("slotCount", server_defaults["slotCount"])
            ).warnings,
        }

    def reveal(self, world: World, index: int) -> dict[str, Any]:
        roles = _roles(self.read(world))
        if not 0 <= index < len(roles):
            raise ConfigError(f"No role at position {index}", 404)
        return {"index": index, "name": roles[index].get("name"),
                "password": roles[index].get("password", "")}

    def export(self, world: World, include_passwords: bool) -> dict[str, Any]:
        doc = self.read(world)
        return doc if include_passwords else mask_roles(doc)

    # -- saving -------------------------------------------------------------------------

    def save(self, world: World, update: ConfigUpdate, *, source: str = "editor",
             note: str = "", dry_run: bool = False) -> SaveResult:
        world = _fresh(world)
        if update.base_revision is not None and update.base_revision != world.config_revision:
            raise ConfigError(
                "The config changed since you loaded it. Reload to see the latest version.",
                409)
        current = self.read(world)
        managed, problems = self._build(current, update)
        if problems.errors:
            raise ConfigError(_summary(problems.errors), 422, problems.errors)
        remove = ("password",) if update.remove_legacy_password and "password" in current else ()
        preview = _preview(current, managed, remove)
        changes = diff_documents(current, preview)
        if dry_run or not changes:
            return SaveResult(world.config_revision, changes, problems.warnings)
        # Staged: the live file changes when the server next starts (apply_pending), so
        # the running server, which rewrites the file itself on bans, can't undo it.
        revision = self._commit(world, preview, changes, source, note)
        return SaveResult(revision, changes, problems.warnings)

    def _build(self, current: dict[str, Any],
               update: ConfigUpdate) -> tuple[dict[str, Any], Problems]:
        problems = Problems()
        managed: dict[str, Any] = {}
        if update.preset is not None:
            problems.extend(validate_preset(update.preset))
            managed["gameSettingsPreset"] = update.preset
        if update.game_settings is not None:
            problems.extend(validate_game_settings(update.game_settings, partial=True))
            managed["gameSettings"] = {**_effective_game_settings(current),
                                       **update.game_settings}
        if update.server is not None:
            problems.extend(validate_server(update.server))
            managed.update({k: v for k, v in update.server.items()
                            if k in EDITABLE_SERVER_FIELDS})
        slot_count = managed.get("slotCount", current.get("slotCount", 16))
        if update.roles is not None:
            roles, role_problems = self._resolve_roles(current, update.roles)
            problems.extend(role_problems)
            managed["userGroups"] = roles
        else:
            roles = _roles(current)
        if isinstance(slot_count, int):
            problems.extend(validate_roles(roles, slot_count))
        return managed, problems

    def _resolve_roles(self, current: dict[str, Any],
                       roles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Problems]:
        problems = Problems()
        existing = _roles(current)
        out: list[dict[str, Any]] = []
        if not isinstance(roles, list):
            problems.error(["roles"], "must be a list")
            return out, problems
        for i, role in enumerate(roles):
            if not isinstance(role, dict):
                problems.error(["roles", i], "must be an object")
                continue
            resolved: dict[str, Any] = {}
            for key in ROLE_KEY_ORDER:
                if key == "password":
                    password = role.get("password")
                    keep = role.get("keep_password_from")
                    if isinstance(password, str):
                        resolved["password"] = password
                    elif isinstance(keep, int) and 0 <= keep < len(existing):
                        resolved["password"] = existing[keep].get("password", "")
                    else:
                        problems.error(["roles", i, "password"], "Password is missing")
                        resolved["password"] = None
                elif key in role:
                    resolved[key] = role[key]
            for key in ROLE_PERMISSIONS:
                resolved.setdefault(key, False)
            resolved.setdefault("reservedSlots", 0)
            register_secret(resolved.get("password"))
            out.append(resolved)
        return out, problems

    def _commit(self, world: World, doc: dict[str, Any], changes: list[dict[str, Any]],
                source: str, note: str) -> int:
        with session_scope() as s:
            row = s.get(World, world.id)
            if row is None:
                raise ConfigError("World was deleted", 404)
            row.config_revision += 1
            name = doc.get("name")
            if isinstance(name, str) and name.strip():
                row.display_name = name.strip()
            revision = row.config_revision
        self.save_history(world, revision, doc, source=source, note=note,
                          changes=len(changes))
        if self.events:
            self.events.record(world.id, "config_saved", revision=revision, source=source,
                               changes=len(changes))
        return revision

    def write_initial(self, world: World, doc: dict[str, Any]) -> None:
        paths = self.paths(world)
        for role in doc.get("userGroups", []):
            register_secret(role.get("password"))
        atomic_write(paths.config, dumps_config(doc), uid=world.puid, gid=world.pgid)
        self.save_history(world, 1, doc, source="created")

    def set_query_port(self, world: World, port: int) -> int | None:
        """Keep the JSON queryPort equal to the container's port (recreate applies it)."""
        current = self.read(world)
        if current.get("queryPort") == port:
            return None
        managed = {"queryPort": port}
        preview = _preview(current, managed, ())
        changes = diff_documents(current, preview)
        return self._commit(world, preview, changes, "server", "")

    def apply_pending(self, world: World) -> list[dict[str, Any]]:
        """Merge the pending revision into the live file. Call only while the server
        process is stopped: it reads the file at boot and rewrites it from memory when
        a ban changes in game (verified 2026-10-15).

        Only the keys FlameKeeper manages are written; the ban list and anything else
        the server wrote (including on shutdown) are kept. Returns the changes.
        """
        world = _fresh(world)
        pending = self._pending_doc(world)
        if pending is None:
            return []
        live = self.read_live(world)
        managed, remove = _managed(pending), _removed(live, pending)
        changes = diff_documents(live, _preview(live, managed, remove))
        if changes:
            self.renderer.write(self.paths(world).config, managed, uid=world.puid,
                                gid=world.pgid, remove=remove)
            log.info("applied config revision", extra={"data": {
                "world": world.slug, "revision": world.config_revision,
                "changes": len(changes)}})
        return changes

    def container_env(self, world: World) -> dict[str, str]:
        doc = self.read(world)
        managed = {k: doc[k] for k in ("gameSettingsPreset", "gameSettings", "userGroups")
                   if k in doc}
        return self.renderer.container_env(managed)

    # -- import -------------------------------------------------------------------------

    def import_document(self, world: World, doc: Any, *, base_revision: int | None,
                        convert_legacy_password: bool, dry_run: bool) -> SaveResult:
        """Replace settings, server fields and roles from an uploaded file."""
        if not isinstance(doc, dict):
            raise ConfigError("The file must contain a JSON object", 422)
        current = self.read(world)
        warnings: list[dict[str, Any]] = []
        update = ConfigUpdate(base_revision=base_revision)
        if "gameSettingsPreset" in doc:
            update.preset = doc["gameSettingsPreset"]
        gs = doc.get("gameSettings")
        if isinstance(gs, dict):
            known = game_setting_defaults()
            unknown = sorted(gs.keys() - known.keys())
            if unknown:
                warnings.append({"loc": ["gameSettings"],
                                 "msg": f"Ignored unknown settings: {', '.join(unknown)}"})
            update.game_settings = {k: v for k, v in gs.items() if k in known}
        elif gs is not None:
            raise ConfigError("gameSettings must be an object", 422)
        server = {k: doc[k] for k in EDITABLE_SERVER_FIELDS if k in doc}
        if server:
            update.server = server
        for key in ("queryPort", "ip"):
            if key in doc and doc[key] != current.get(key):
                warnings.append({"loc": [key], "msg": f"Ignored {key}: change it on the "
                                                      "Server tab (it recreates the container)"})
        roles_in = doc.get("userGroups")
        if roles_in is not None:
            update.roles = self._import_roles(current, roles_in)
        legacy = doc.get("password")
        if isinstance(legacy, str) and legacy and legacy != MASK:
            if convert_legacy_password:
                roles = update.roles if update.roles is not None else [
                    {**r, "keep_password_from": i} for i, r in enumerate(_roles(current))]
                roles.append({**_legacy_role(), "password": legacy})
                update.roles = roles
            else:
                warnings.append({"loc": ["password"],
                                 "msg": "The file has a legacy top-level password. It is not "
                                        "imported; tick 'convert to a role' to keep it."})
        result = self.save(world, update, source="import", dry_run=dry_run)
        result.warnings = warnings + result.warnings
        return result

    def _import_roles(self, current: dict[str, Any], roles: Any) -> list[dict[str, Any]]:
        if not isinstance(roles, list):
            raise ConfigError("userGroups must be a list", 422)
        by_name = {r.get("name"): i for i, r in enumerate(_roles(current))}
        out = []
        for i, role in enumerate(roles):
            if not isinstance(role, dict):
                raise ConfigError(f"userGroups[{i}] must be an object", 422)
            role = dict(role)
            if role.get("password") == MASK:
                # A masked export: keep the password the role has now.
                if role.get("name") not in by_name:
                    raise ConfigError(
                        f"Role {role.get('name')!r} has a masked password and doesn't exist "
                        "in this world. Export with passwords included, or set one after "
                        "importing.", 422, [{"loc": ["roles", i, "password"],
                                             "msg": "masked password for a new role"}])
                role.pop("password")
                role["keep_password_from"] = by_name[role.get("name")]
            out.append(role)
        return out

    def convert_legacy_password(self, world: World, base_revision: int | None) -> SaveResult:
        """Turn a legacy top-level `password` into a role and delete the key."""
        current = self.read(world)
        legacy = current.get("password")
        if not isinstance(legacy, str) or not legacy:
            raise ConfigError("There is no legacy password to convert", 409)
        roles = [{**r, "keep_password_from": i} for i, r in enumerate(_roles(current))]
        roles.append({**_legacy_role(), "password": legacy})
        return self.save(world, ConfigUpdate(base_revision=base_revision, roles=roles,
                                             remove_legacy_password=True), source="legacy")

    # -- revisions ----------------------------------------------------------------------

    def save_history(self, world: World, revision: int, doc: dict[str, Any], *,
                     source: str = "editor", note: str = "", changes: int | None = None) -> None:
        history = self.paths(world).history
        history.mkdir(parents=True, exist_ok=True)
        meta = {"revision": revision, "saved_at": datetime.now(UTC).isoformat(),
                "source": source, "note": note, "changes": changes}
        _write_private(history / f"{revision}.json", json.dumps(doc, indent="\t"))
        _write_private(history / f"{revision}.meta.json", json.dumps(meta))
        for old in self._revision_numbers(world)[:-HISTORY_KEEP]:
            (history / f"{old}.json").unlink(missing_ok=True)
            (history / f"{old}.meta.json").unlink(missing_ok=True)

    def _revision_numbers(self, world: World) -> list[int]:
        history = self.paths(world).history
        if not history.is_dir():
            return []
        return sorted(int(p.stem) for p in history.glob("*.json") if p.stem.isdigit())

    def _history_doc(self, world: World, revision: int) -> dict[str, Any]:
        path = self.paths(world).history / f"{revision}.json"
        try:
            doc = json.loads(path.read_text())
        except FileNotFoundError:
            raise ConfigError(f"Revision {revision} is not in the history "
                              f"(only the last {HISTORY_KEEP} are kept)", 404) from None
        for role in _roles(doc):
            register_secret(role.get("password"))
        return doc

    def _history_meta(self, world: World, revision: int) -> dict[str, Any]:
        history = self.paths(world).history
        try:
            return json.loads((history / f"{revision}.meta.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            path = history / f"{revision}.json"
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC) if path.exists() else None
            return {"revision": revision, "saved_at": mtime.isoformat() if mtime else None,
                    "source": "created" if revision == 1 else "editor", "note": "",
                    "changes": None}

    def revisions(self, world: World) -> list[dict[str, Any]]:
        world = _fresh(world)
        out = []
        for rev in reversed(self._revision_numbers(world)):
            meta = self._history_meta(world, rev)
            out.append({**meta, "revision": rev,
                        "current": rev == world.config_revision,
                        "applied": rev == world.applied_revision})
        return out

    def revision(self, world: World, revision: int) -> dict[str, Any]:
        world = _fresh(world)
        doc = self._history_doc(world, revision)
        numbers = self._revision_numbers(world)
        earlier = [n for n in numbers if n < revision]
        previous = self._history_doc(world, earlier[-1]) if earlier else None
        current = self.read(world)
        return {
            **self._history_meta(world, revision),
            "revision": revision,
            "previous_revision": earlier[-1] if earlier else None,
            "changes": diff_documents(previous, doc) if previous is not None else [],
            # What restoring this revision would change in the current file.
            "restore_changes": diff_documents(current, _restorable(current, doc)),
            "document": mask_roles(doc),
        }

    def restore(self, world: World, revision: int, base_revision: int | None) -> SaveResult:
        doc = self._history_doc(world, revision)
        return self.apply_document(world, doc, base_revision=base_revision, source="restore")

    def apply_document(self, world: World, doc: dict[str, Any], *,
                       base_revision: int | None = None, source: str, note: str = "",
                       keep_name: bool = False) -> SaveResult:
        """Save preset, game settings, server fields and roles (with their passwords)
        from a whole document, e.g. a history revision or the config inside a backup.
        The port, bind address, ban list and unknown keys of the current file stay."""
        roles = _roles(doc)
        server = {k: doc[k] for k in EDITABLE_SERVER_FIELDS if k in doc}
        if keep_name:
            server.pop("name", None)
        update = ConfigUpdate(
            base_revision=base_revision,
            preset=doc.get("gameSettingsPreset"),
            game_settings={k: v for k, v in (doc.get("gameSettings") or {}).items()
                           if k in game_setting_defaults()},
            server=server or None,
            roles=[{**r, "password": r.get("password", "")} for r in roles] if roles else None,
        )
        return self.save(world, update, source=source, note=note)


# Top-level keys FlameKeeper owns. Everything else (bans, directories, unknown keys) is
# the server's and is never written from a revision.
MANAGED_KEYS = ("gameSettingsPreset", "gameSettings", "userGroups", "queryPort",
                *EDITABLE_SERVER_FIELDS)


def _managed(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: doc[k] for k in MANAGED_KEYS if k in doc}


def _removed(live: dict[str, Any], saved: dict[str, Any]) -> tuple[str, ...]:
    """The legacy password is the one key a revision can delete (convert to a role)."""
    return ("password",) if "password" in live and "password" not in saved else ()


def _register(doc: dict[str, Any]) -> None:
    for role in _roles(doc):
        register_secret(role.get("password"))
    if isinstance(doc.get("password"), str):
        register_secret(doc["password"])


def _legacy_role() -> dict[str, Any]:
    return {"name": LEGACY_ROLE_NAME, **{k: v for k, v in _FRIEND.items() if k != "name"}}


def _restorable(current: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    from app.renderers.json_file import merge_server_config

    managed: dict[str, Any] = {k: doc[k] for k in ("gameSettingsPreset", "userGroups",
                                                   *EDITABLE_SERVER_FIELDS) if k in doc}
    if isinstance(doc.get("gameSettings"), dict):
        managed["gameSettings"] = doc["gameSettings"]
    return merge_server_config(current, managed)


def _preview(current: dict[str, Any], managed: dict[str, Any],
             remove: tuple[str, ...]) -> dict[str, Any]:
    from app.renderers.json_file import merge_server_config

    return merge_server_config(current, managed, remove)


def _summary(errors: list[dict[str, Any]]) -> str:
    first = errors[0]["msg"]
    return first if len(errors) == 1 else f"{first} (and {len(errors) - 1} more problems)"


def _fresh(world: World) -> World:
    with session_scope() as s:
        row = s.get(World, world.id)
        if row is None:
            raise ConfigError("World was deleted", 404)
        s.expunge(row)
        return row


def _write_private(path: Path, content: str) -> None:
    """History files hold plaintext passwords: readable by the controller only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
