"""Importing a server FlameKeeper didn't create: a container of any Enshrouded image, a
server folder on the host, or a backup zip.

Every source ends up as one backup zip (kind "imported") in the new world, holding the
server's savegame folder and enshrouded_server.json. The world is created from that zip
the same way clone does, so the original files are always kept as the world's first
backup.

Files are found by name, not by image, so any image works:

- container: `find` over its mounts in a helper container (--volumes-from), then the
  config next to the save folder or in a few known install folders. Files are read with
  docker cp, which also reaches files outside the mounts and works on stopped containers.
- host folder: `find`, `cat` and `cp` in a helper container with the host's / mounted
  read-only (mounting the folder itself would create it on the host if it's missing).

A scan is kept in DATA_DIR/imports/<token>/ until the import succeeds (or 24 hours).

A running container is stopped during the import job, after the new world's files are
in place and just before its first start, and its restart policy is set to "no" so it
doesn't come back after a reboot and take the port. If the import fails, the old server
gets its restart policy back and is started again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import secrets
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from app.config import APP_NAME, LABEL_MANAGED, STEAM_APP_ID, Settings
from app.db import session_scope
from app.models import World, WorldImport
from app.renderers.json_file import atomic_write, dumps_config
from app.services import backup_archive as archive
from app.services.backup_service import BackupService
from app.services.capacity import GAME_DOWNLOAD
from app.services.config_service import LEGACY_ROLE_NAME, MASK
from app.services.config_validation import (
    EDITABLE_SERVER_FIELDS,
    ROLE_PERMISSIONS,
    server_fields_by_key,
    settings_by_key,
    validate_preset,
    validate_roles,
    validate_setting,
)
from app.services.docker_adapter import ContainerInfo, DockerAdapter, DockerError
from app.services.events import EventLog
from app.services.jobs import JobContext, JobFailed, JobManager
from app.services.log_patterns import load_patterns
from app.services.world_paths import container_name, parse_buildid
from app.services.world_service import CreateWorldInput, WorldError, WorldService

log = logging.getLogger(__name__)

CONFIG_NAME = archive.CONFIG_NAME
# File names `find` looks for; the results are filtered with the save-name patterns.
FIND_NAMES = (CONFIG_NAME, "world*.ksd", "????????", "????????-index")
FIND_DEPTH = 8
# Where images install the server, for a config outside the container's mounts.
KNOWN_INSTALL_DIRS = ("/opt/enshrouded/server", "/home/steam/enshrouded",
                      "/home/steam/enshrouded-server", "/home/enshrouded/server",
                      "/enshrouded", "/server", "/data")
GAME_PORTS = ("15636/udp", "15637/udp")
DEFAULT_QUERY_PORT = 15637
HOST_ROOT = "/host"
STALE_AFTER = timedelta(hours=24)
STOP_TIMEOUT_S = 120
MAX_CONFIG_BYTES = 1024**2
LOG_LINES = 5000
_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
GAME_EXE = "enshrouded_server.exe"
# Copied with the save and config, or the server's own output: not game files.
NOT_GAME_FILES = frozenset({"savegame", "logs", "backups", "crash_dumps", CONFIG_NAME})
GAME_COPY_TIMEOUT_S = 2 * 60 * 60
# Game files from a host folder; $1 = install folder, $2 = save folder name to skip.
GAME_COPY_SCRIPT = ('cd "$1" || exit 1; for f in * .[!.]*; do '
                    '{ [ -e "$f" ] || [ -L "$f" ]; } || continue; '
                    'case "$f" in savegame|logs|backups|crash_dumps|enshrouded_server.json) '
                    'continue;; esac; [ "$f" = "$2" ] && continue; '
                    'cp -a "$f" /out/ || exit 1; done')
COPY_SCRIPT = ('set -e; mkdir -p /out/savegame; cp -R "$1"/. /out/savegame/; '
               'if [ -n "$2" ]; then cp "$2" /out/enshrouded_server.json; fi')


def _now() -> datetime:
    return datetime.now(UTC)


def _find_cmd(roots: list[str]) -> list[str]:
    names: list[str] = []
    for n in FIND_NAMES:
        names += ["-o", "-name", n] if names else ["-name", n]
    return ["find", *roots, "-maxdepth", str(FIND_DEPTH), "-type", "f", "(", *names, ")"]


def _parse_find(output: str) -> tuple[list[str], set[PurePosixPath]]:
    """(config paths, save folders) from `find` output."""
    configs, saves = [], []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("/"):
            continue  # "find: …: Permission denied" and the like
        p = PurePosixPath(line)
        if p.name == CONFIG_NAME:
            configs.append(line)
        elif archive.is_main_save(p.name):
            saves.append(p)
    return sorted(configs), archive.save_folders(saves)


def _save_dir_of(config_path: str, doc: dict[str, Any]) -> str:
    """The save folder a config points at (relative paths are from the config's folder,
    the server's working directory)."""
    rel = doc.get("saveDirectory")
    if not isinstance(rel, str) or not rel.strip() or "\\" in rel or ":" in rel:
        rel = "./savegame"
    return posixpath.normpath(posixpath.join(posixpath.dirname(config_path), rel))


def sanitize(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Make an imported config fit FlameKeeper: drop values it would reject (the world
    gets the default instead) and clamp the slot count. Returns (doc, warnings, errors);
    errors (roles) block the import."""
    out = dict(doc)
    warnings: list[str] = []
    errors: list[str] = []
    if "gameSettingsPreset" in out and validate_preset(out["gameSettingsPreset"]).errors:
        warnings.append(f"Unknown difficulty preset {out.pop('gameSettingsPreset')!r}: "
                        "using Default")
    gs = out.get("gameSettings")
    if isinstance(gs, dict):
        known = settings_by_key()
        kept = {}
        for key, value in gs.items():
            if key not in known:
                warnings.append(f"Ignored unknown game setting {key}")
            elif msg := validate_setting(known[key], value):
                warnings.append(f"{known[key]['label']} {msg}: using the default")
            else:
                kept[key] = value
        out["gameSettings"] = kept
    elif gs is not None:
        warnings.append("gameSettings is not an object: using the defaults")
        out.pop("gameSettings")
    slots = out.get("slotCount")
    if isinstance(slots, int) and not isinstance(slots, bool) and slots > 16:
        warnings.append(f"Max players was {slots}; Enshrouded allows 16")
        out["slotCount"] = 16
    fields = server_fields_by_key()
    for key in EDITABLE_SERVER_FIELDS:
        if key in out and key != "name" and (msg := validate_setting(fields[key], out[key])):
            warnings.append(f"{fields[key]['label']} {msg}: using the default")
            out.pop(key)
    roles = [r for r in out.get("userGroups") or [] if isinstance(r, dict)]
    legacy = out.get("password")
    if isinstance(legacy, str) and legacy and legacy != MASK:
        roles = [*roles, {"name": LEGACY_ROLE_NAME, "password": legacy}]
        warnings.append(f"The server password becomes a role named {LEGACY_ROLE_NAME!r}")
    if roles:
        full = [{**{p: False for p in ROLE_PERMISSIONS}, "reservedSlots": 0, **r}
                for r in roles]
        slot_count = out.get("slotCount") if isinstance(out.get("slotCount"), int) else 16
        for e in validate_roles(full, slot_count).errors:
            loc = e["loc"]
            who = (f"Role {roles[loc[1]].get('name')!r}: " if len(loc) > 1
                   and isinstance(loc[1], int) else "Roles: ")
            errors.append(who + e["msg"])
    else:
        warnings.append("The server has no roles: the world gets FlameKeeper's default "
                        "roles with new passwords")
    return out, warnings, errors


def describe(doc: dict[str, Any] | None, summary: archive.ZipSummary,
             zip_path: Path) -> dict[str, Any]:
    """What the UI shows about a scanned server (no passwords)."""
    doc = doc or {}
    names = [PurePosixPath(f).name for f in summary.save_files]
    saved_at = None
    with archive.zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.filename in summary.save_files:
                t = datetime(*info.date_time, tzinfo=UTC)
                saved_at = t if saved_at is None or t > saved_at else saved_at
    roles = [r for r in doc.get("userGroups") or [] if isinstance(r, dict)]
    gs = doc.get("gameSettings") if isinstance(doc.get("gameSettings"), dict) else {}
    bans = sum(len(doc[k]) for k in ("bannedAccounts", "bans") if isinstance(doc.get(k), list))
    return {
        "has_config": bool(doc),
        "name": doc.get("name") if isinstance(doc.get("name"), str) else None,
        "slot_count": doc.get("slotCount") if isinstance(doc.get("slotCount"), int) else None,
        "query_port": doc.get("queryPort") if isinstance(doc.get("queryPort"), int) else None,
        "preset": doc.get("gameSettingsPreset"),
        "custom_settings": len(gs),
        "roles": [{"name": r.get("name"), "has_password": bool(r.get("password")),
                   "reserved_slots": r.get("reservedSlots", 0)} for r in roles],
        "legacy_password": bool(doc.get("password")),
        "bans": bans,
        "save_format": "1.0" if any(n.startswith("world") for n in names) else "pre-1.0",
        "save_files": len(summary.save_files),
        "save_bytes": summary.uncompressed_bytes,
        "saved_at": saved_at.isoformat() if saved_at else None,
    }


class ImportService:
    def __init__(self, settings: Settings, docker: DockerAdapter, worlds: WorldService,
                 backups: BackupService, jobs: JobManager, events: EventLog) -> None:
        self.settings = settings
        self.docker = docker
        self.worlds = worlds
        self.backups = backups
        self.jobs = jobs
        self.events = events
        self._pending: dict[str, tuple[str, str]] = {}  # job id -> (world id, token)
        self._tasks: set[asyncio.Task[None]] = set()
        jobs.listeners.append(self._on_job)

    # -- candidates ---------------------------------------------------------------------

    async def candidates(self) -> list[dict[str, Any]]:
        """Containers FlameKeeper doesn't manage, likely Enshrouded servers first."""
        with session_scope() as s:
            imported = {r.container_id: r.world_id for r in s.scalars(select(WorldImport))
                        if r.container_id and r.state not in ("rolled_back",)}
        slugs = {w.id: w.slug for w in self.worlds.list()}
        out = []
        for c in await self.docker.list_containers():
            if c.labels.get(LABEL_MANAGED) or c.labels.get(f"{LABEL_MANAGED}.helper"):
                continue
            likely = ("enshrouded" in c.image.lower()
                      or any(p in c.ports for p in GAME_PORTS)
                      or any("enshrouded" in m.destination.lower() for m in c.mounts))
            out.append({
                "id": c.id,
                "name": c.name,
                "image": c.image,
                "status": c.status,
                "running": c.running,
                "likely": likely,
                "ports": c.ports,
                "mounts": [{"type": m.type, "name": m.name, "source": m.source,
                            "destination": m.destination} for m in c.mounts],
                "restart_policy": c.restart_policy,
                "imported_into": slugs.get(imported.get(c.id, "")),
            })
        out.sort(key=lambda c: (not c["likely"], c["name"]))
        return out

    async def players_online(self, c: ContainerInfo) -> int | None:
        """Players online by the server's log lines; None when the log doesn't say
        (an image that doesn't print the game log)."""
        if not c.running:
            return 0
        patterns = load_patterns()
        online: set[str] = set()
        known = False
        for line in await self.docker.logs_tail(c.id, tail=LOG_LINES):
            ev = patterns.classify(line)
            if ev is None:
                continue
            if ev.kind == "host_online":
                online.clear()
                known = True
            elif ev.kind == "player_joined":
                online.add(ev.data["name"])
                known = True
            elif ev.kind == "player_left":
                online.discard(ev.data["name"])
        return len(online) if known else None

    # -- scanning -----------------------------------------------------------------------

    def _dir(self, token: str) -> Path:
        if not _TOKEN_RE.match(token):
            raise WorldError("Import not found", 404)
        return self.settings.imports_dir / token

    def _new_token(self) -> tuple[str, Path]:
        self.prune()
        token = secrets.token_hex(8)
        folder = self.settings.imports_dir / token
        folder.mkdir(parents=True, mode=0o700)
        return token, folder

    def prune(self) -> None:
        """Delete scans older than STALE_AFTER that no running import uses."""
        root = self.settings.imports_dir
        if not root.is_dir():
            return
        cutoff = (_now() - STALE_AFTER).timestamp()
        for folder in root.iterdir():
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)

    def upload_path(self) -> tuple[str, Path]:
        """Where the API streams an uploaded zip before scan_upload."""
        token, folder = self._new_token()
        return token, folder / "upload.zip"

    async def scan_container(self, name_or_id: str) -> dict[str, Any]:
        c = await self.docker.get_container(name_or_id)
        if c is None:
            raise WorldError(f"Container {name_or_id!r} not found", 404)
        if c.labels.get(LABEL_MANAGED):
            raise WorldError("FlameKeeper already manages this container", 409)
        token, folder = self._new_token()
        try:
            source = {"kind": "container", "container_id": c.id, "container_name": c.name,
                      "image": c.image, "label": c.name}
            found = await self._locate_in_container(c, folder)
            scan = await self._finish_scan(token, folder, source, *found)
            scan.update(await self._container_facts(c, scan))
            scan["game_files"] = await self._game_files_in_container(c, scan, folder)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self._save(folder, scan)
        return scan

    async def scan_host_path(self, path: str) -> dict[str, Any]:
        path = path.strip()
        if not path.startswith("/"):
            raise WorldError("Enter an absolute path, e.g. /srv/enshrouded", 422)
        path = posixpath.normpath(path)
        if path == "/":
            raise WorldError("Choose the server's folder, not /", 422)
        token, folder = self._new_token()
        try:
            source = {"kind": "host_path", "host_path": path, "label": path}
            found = await self._locate_on_host(path)
            scan = await self._finish_scan(token, folder, source, *found)
            scan.update({"port": await self._free_port(scan["summary"]["query_port"]),
                         "reusable_port": None, "running": False, "players_online": None})
            scan["game_files"] = await self._game_files_on_host(scan)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self._save(folder, scan)
        return scan

    async def scan_upload(self, token: str, filename: str) -> dict[str, Any]:
        folder = self._dir(token)
        zip_path = folder / "upload.zip"
        try:
            summary = await asyncio.to_thread(archive.inspect_zip, zip_path)
            doc = await asyncio.to_thread(archive.read_config, zip_path)
        except (archive.ArchiveError, json.JSONDecodeError, UnicodeDecodeError) as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise WorldError(f"Can't import this zip: {e}", 422) from e
        scan = self._scan_record(token, {"kind": "upload", "filename": filename,
                                         "label": filename}, None, summary.save_prefix,
                                 [], doc, summary, zip_path)
        scan.update({"port": await self._free_port(scan["summary"]["query_port"]),
                     "reusable_port": None, "running": False, "players_online": None,
                     "game_files": None})
        self._save(folder, scan)
        return scan

    def _install_candidates(self, scan: dict[str, Any], *, known: bool) -> list[str]:
        """Where the server's install (enshrouded_server.exe) may be: next to the config,
        above the save folder, then (in containers) where images install it."""
        dirs = []
        if scan["config_path"]:
            dirs.append(posixpath.dirname(scan["config_path"]))
        if scan["save_path"]:
            dirs.append(posixpath.dirname(scan["save_path"]))
        if known:
            dirs += KNOWN_INSTALL_DIRS
        return list(dict.fromkeys(dirs))

    async def _game_files_in_container(self, c: ContainerInfo, scan: dict[str, Any],
                                       folder: Path) -> dict[str, Any] | None:
        for d in self._install_candidates(scan, known=True):
            try:
                if not await self.docker.path_exists(c.id, f"{d}/{GAME_EXE}"):
                    continue
            except DockerError:
                continue
            acf = await self._read_container_file(
                c, f"{d}/steamapps/appmanifest_{STEAM_APP_ID}.acf", folder)
            return {"path": d, "build": parse_buildid(acf.decode(errors="replace"))
                    if acf else None}
        return None

    async def _game_files_on_host(self, scan: dict[str, Any]) -> dict[str, Any] | None:
        binds = {"/": f"{HOST_ROOT}:ro"}
        for d in self._install_candidates(scan, known=False):
            try:
                res = await self.docker.run_helper(
                    self.settings.helper_image, ["test", "-f", f"{HOST_ROOT}{d}/{GAME_EXE}"],
                    binds=binds)
                if res.exit_code != 0:
                    continue
                acf = await self.docker.run_helper(
                    self.settings.helper_image,
                    ["cat", f"{HOST_ROOT}{d}/steamapps/appmanifest_{STEAM_APP_ID}.acf"],
                    binds=binds)
            except DockerError:
                continue
            build = parse_buildid(acf.output) if acf.exit_code == 0 else None
            return {"path": d, "build": build}
        return None

    async def _locate_in_container(self, c: ContainerInfo, folder: Path
                                   ) -> tuple[str | None, str | None, list[str], bytes | None]:
        roots = sorted({m.destination for m in c.mounts if m.type in ("bind", "volume")
                        and not m.destination.endswith(".sock")})
        configs: list[str] = []
        folders: set[PurePosixPath] = set()
        if roots:
            try:
                res = await self.docker.run_helper(self.settings.helper_image, _find_cmd(roots),
                                                   volumes_from=[c.id])
            except DockerError as e:
                raise WorldError(f"Could not search the container's volumes: {e}", 502) from e
            configs, folders = _parse_find(res.output)
        # A config outside the mounts (in the container's own files): next to a save
        # folder, then where images install the server.
        probes = configs + [str(f.parent / CONFIG_NAME) for f in sorted(folders)]
        probes += [f"{d}/{CONFIG_NAME}" for d in KNOWN_INSTALL_DIRS]
        docs: dict[str, dict[str, Any]] = {}
        raw: dict[str, bytes] = {}
        for path in dict.fromkeys(probes):
            data = await self._read_container_file(c, path, folder)
            if data is None:
                continue
            try:
                doc = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(doc, dict):
                docs[path], raw[path] = doc, data
            if len(docs) >= 5:
                break
        # The save folder a config points at, when `find` couldn't see it.
        for path, doc in docs.items():
            target = PurePosixPath(_save_dir_of(path, doc))
            if target not in folders and await self._container_has_save(c, str(target),
                                                                         folder):
                folders.add(target)
        config, save = self._pair(docs, folders)
        return config, save, sorted(str(f) for f in folders), raw.get(config or "")

    async def _read_container_file(self, c: ContainerInfo, path: str,
                                   folder: Path) -> bytes | None:
        tmp = folder / "probe.tar"
        try:
            await self.docker.get_archive(c.id, path, tmp)

            def read() -> bytes | None:
                with tarfile.open(tmp) as tar:
                    m = next((m for m in tar.getmembers() if m.isfile()), None)
                    if m is None or m.size > MAX_CONFIG_BYTES:
                        return None
                    f = tar.extractfile(m)
                    return f.read() if f else None

            return await asyncio.to_thread(read)
        except (DockerError, tarfile.TarError, OSError):
            return None
        finally:
            tmp.unlink(missing_ok=True)

    async def _container_has_save(self, c: ContainerInfo, path: str, folder: Path) -> bool:
        tmp = folder / "probe.tar"
        try:
            await self.docker.get_archive(c.id, path, tmp)

            def check() -> bool:
                with tarfile.open(tmp) as tar:
                    return any(m.isfile() and archive.is_main_save(PurePosixPath(m.name).name)
                               for m in tar.getmembers())

            return await asyncio.to_thread(check)
        except (DockerError, tarfile.TarError, OSError):
            return False
        finally:
            tmp.unlink(missing_ok=True)

    async def _locate_on_host(self, path: str
                              ) -> tuple[str | None, str | None, list[str], bytes | None]:
        root = HOST_ROOT + path
        try:
            res = await self.docker.run_helper(self.settings.helper_image, _find_cmd([root]),
                                               binds={"/": f"{HOST_ROOT}:ro"})
        except DockerError as e:
            raise WorldError(f"Could not search {path}: {e}", 502) from e
        configs, folders = _parse_find(res.output)
        docs: dict[str, dict[str, Any]] = {}
        raw: dict[str, bytes] = {}
        for config in configs[:5]:
            try:
                out = await self.docker.run_helper(self.settings.helper_image,
                                                   ["cat", config],
                                                   binds={"/": f"{HOST_ROOT}:ro"})
                doc = json.loads(out.output)
            except (DockerError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict):
                docs[config], raw[config] = doc, out.output.encode()
        config, save = self._pair(docs, folders)

        def host(p: str | None) -> str | None:  # as the user knows the path
            return p[len(HOST_ROOT):] if p and p.startswith(HOST_ROOT + "/") else p

        return (host(config), host(save), sorted(str(host(str(f))) for f in folders),
                raw.get(config or ""))

    @staticmethod
    def _pair(docs: dict[str, dict[str, Any]], folders: set[PurePosixPath]
              ) -> tuple[str | None, str | None]:
        """The config and the save folder it uses; else the only (or first) of each."""
        for path, doc in docs.items():
            target = PurePosixPath(_save_dir_of(path, doc))
            if target in folders:
                return path, str(target)
        config = next(iter(docs), None)
        save = str(sorted(folders)[0]) if folders else None
        return config, save

    async def _finish_scan(self, token: str, folder: Path, source: dict[str, Any],
                           config: str | None, save: str | None, options: list[str],
                           config_raw: bytes | None) -> dict[str, Any]:
        if save is None:
            where = source.get("host_path") or f"container {source.get('container_name')}"
            raise WorldError(f"No Enshrouded savegame found in {where} (looked for "
                             "world0.ksd, or 3ad85aea and 3ad85aea-index)", 422)
        zip_path = folder / "scan.zip"
        await self._snapshot(source, save, config, config_raw, folder / "stage", zip_path,
                             note="Scan")
        summary = await asyncio.to_thread(archive.inspect_zip, zip_path, trusted=True)
        doc = await asyncio.to_thread(archive.read_config, zip_path)
        return self._scan_record(token, source, config, save, options, doc, summary, zip_path)

    def _scan_record(self, token: str, source: dict[str, Any], config: str | None,
                     save: str | None, options: list[str], doc: dict[str, Any] | None,
                     summary: archive.ZipSummary, zip_path: Path) -> dict[str, Any]:
        warnings = list(summary.warnings)
        errors: list[str] = []
        if doc is None:
            warnings.append("No enshrouded_server.json found: the world gets FlameKeeper's "
                            "default settings and roles")
        else:
            _, more, errors = sanitize(doc)
            warnings += more
        if len(options) > 1:
            warnings.append("Savegames were found in more than one folder; check the one "
                            "to import")
        return {
            "token": token,
            "created_at": _now().isoformat(),
            "source": source,
            "config_path": config,
            "save_path": save,
            "save_options": options,
            "summary": describe(doc, summary, zip_path),
            "warnings": warnings,
            "errors": errors,
        }

    async def _container_facts(self, c: ContainerInfo, scan: dict[str, Any]
                               ) -> dict[str, Any]:
        query = scan["summary"]["query_port"] or DEFAULT_QUERY_PORT
        if c.network_mode == "host":
            held: int | None = query
        else:
            hosts = c.ports.get(f"{query}/udp") or []
            held = hosts[0] if hosts else None
        port = held if held is not None and not self._world_port(held) \
            else await self._free_port(None)
        return {
            "port": port,
            # The old server holds this port; it's stopped before the new one starts.
            "reusable_port": held,
            "running": c.running,
            "auto_remove": c.auto_remove,
            "restart_policy": c.restart_policy,
            "players_online": await self.players_online(c),
            "volumes": sorted({m.name for m in c.mounts if m.type == "volume" and m.name}),
            "bind_sources": sorted({m.source for m in c.mounts if m.type == "bind"
                                    and not m.destination.endswith(".sock")}),
        }

    def _world_port(self, port: int) -> bool:
        return any(w.query_port == port for w in self.worlds.list())

    async def _free_port(self, wanted: int | None) -> int:
        if wanted and 1024 <= wanted <= 65535 and wanted not in await self.worlds.used_ports():
            return wanted
        return await self.worlds.suggest_port()

    async def _snapshot(self, source: dict[str, Any], save: str, config: str | None,
                        config_raw: bytes | None, stage: Path, dest: Path, *,
                        note: str) -> None:
        """Copy the save folder and config out of the source into a backup zip. `stage`
        names a scratch folder; a fresh one is used each time (files the helper copies
        are root's, which a non-root controller can't clean up)."""
        stage = Path(tempfile.mkdtemp(prefix=f".{stage.name}-", dir=stage.parent))
        try:
            if source["kind"] == "container":
                tar = stage.parent / "save.tar"
                try:
                    await self.docker.get_archive(source["container_id"], save, tar)
                    await asyncio.to_thread(archive.extract_tar, tar, stage / "savegame")
                finally:
                    tar.unlink(missing_ok=True)
                if config:
                    config_raw = await self._read_container_file(
                        await self._container(source), config, stage.parent)
            else:
                res = await self.docker.run_helper(
                    self.settings.helper_image,
                    ["sh", "-c", COPY_SCRIPT, "sh", HOST_ROOT + save,
                     HOST_ROOT + config if config else ""],
                    binds={"/": f"{HOST_ROOT}:ro", str(stage): "/out"})
                if res.exit_code != 0:
                    raise WorldError(f"Could not copy the savegame: {res.output.strip()}", 502)
                cfg = stage / CONFIG_NAME
                config_raw = cfg.read_bytes() if cfg.exists() else None
                cfg.unlink(missing_ok=True)
            saved = archive.latest_mtime(stage / "savegame")
            manifest = {
                "app": APP_NAME,
                "app_version": self.settings.flamekeeper_version,
                "created_at": _now().isoformat(),
                "kind": "imported",
                "note": note,
                "saved_at": saved.isoformat() if saved else None,
                "source": {k: v for k, v in source.items() if k != "label"},
            }
            await asyncio.to_thread(archive.write_backup, dest, stage / "savegame",
                                    config_raw, manifest)
        except (DockerError, archive.ArchiveError, OSError) as e:
            raise WorldError(f"Could not copy the savegame: {e}", 502) from e
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def _container(self, source: dict[str, Any]) -> ContainerInfo:
        c = await self.docker.get_container(source["container_id"])
        if c is None:
            raise WorldError(f"Container {source['container_name']} no longer exists", 404)
        return c

    def _save(self, folder: Path, scan: dict[str, Any]) -> None:
        path = folder / "scan.json"
        path.write_text(json.dumps(scan, indent=2))
        path.chmod(0o600)

    def load(self, token: str) -> dict[str, Any]:
        path = self._dir(token) / "scan.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise WorldError("This scan has expired; scan the server again", 404) from e

    # -- importing ----------------------------------------------------------------------

    async def create_input(self, scan: dict[str, Any], body: dict[str, Any]
                           ) -> CreateWorldInput:
        slots = scan["summary"]["slot_count"] or 16
        return CreateWorldInput(
            display_name=body["display_name"], slug=body.get("slug"),
            slot_count=min(max(slots, 1), 16), query_port=body.get("query_port"),
            image_tag=body.get("image_tag"), game_branch=body.get("game_branch"),
            update_policy=body.get("update_policy"), start=body.get("start", True),
            copy_game_from=body.get("copy_game_from"),
            reusable_port=scan.get("reusable_port"))

    async def check(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Everything create checks, without changing anything."""
        scan = self.load(token)
        if scan["errors"]:
            raise WorldError("Fix these problems in the old server's config first: "
                             + "; ".join(scan["errors"]), 422)
        if body.get("copy_game_from_source"):
            if not scan.get("game_files"):
                raise WorldError("The scan didn't find the old server's game files", 422)
            if body.get("copy_game_from"):
                raise WorldError("Copy game files from the old server or from a world, "
                                 "not both", 422)
        save = body.get("save_path") or scan["save_path"]
        if save != scan["save_path"] and save not in scan["save_options"]:
            raise WorldError("Choose one of the save folders the scan found", 422)
        if scan["source"]["kind"] == "container":
            c = await self._container(scan["source"])
            if c.running and c.auto_remove:
                raise WorldError(f"{c.name} was started with --rm, so stopping it would delete "
                                 "it. Stop it yourself, then scan it again.", 409)
            players = await self.players_online(c)
            if c.running and players != 0 and not body.get("confirm_players"):
                msg = (f"{players} player(s) are online on {c.name}" if players
                       else f"FlameKeeper can't tell whether players are online on {c.name}")
                raise WorldError(f"{msg}; the import stops it. Confirm to go ahead.", 409)
        return {**scan, "save_path": save}

    async def create(self, token: str, body: dict[str, Any]) -> tuple[World, str]:
        scan = await self.check(token, body)
        folder = self._dir(token)
        source = scan["source"]
        label = source["label"]
        note = f"Imported from {label}"

        async def seed(ctx: JobContext, new: World) -> None:
            zip_path = folder / "import.zip"
            if source["kind"] == "upload":
                shutil.copy2(folder / "upload.zip", zip_path)
            else:
                if source["kind"] == "container":
                    await self._stop_source(ctx, new, source)
                ctx.progress(2, f"Copying the savegame and settings from {label}")
                try:
                    await self._snapshot(source, scan["save_path"], scan["config_path"], None,
                                         folder / "stage", zip_path, note=note)
                except WorldError as e:
                    raise JobFailed(str(e)) from e
            dest = self.backups.add_file(new, zip_path, kind="imported", note=note)
            ctx.log(f"Kept the imported files as backup {dest.name}")
            try:
                summary = await asyncio.to_thread(archive.inspect_zip, dest,
                                                  trusted=source["kind"] != "upload")
                doc = await asyncio.to_thread(archive.read_config, dest) or {}
            except archive.ArchiveError as e:
                raise JobFailed(f"Can't import: {e}") from e
            clean, warnings, errors = sanitize(doc) if doc else ({}, [], [])
            for w in warnings:
                ctx.log(f"Note: {w}")
            if errors:
                raise JobFailed("The old server's config has problems: " + "; ".join(errors))
            await self.backups.seed_from_zip(ctx, new, dest, summary, clean, source="import",
                                             note=note, convert_legacy_password=True)
            self._copy_bans(ctx, new, doc)

        game = scan.get("game_files") if body.get("copy_game_from_source") else None

        async def copy_game(ctx: JobContext, new: World) -> None:
            await self._copy_game_files(ctx, new, source, game["path"], scan["save_path"])

        world, job_id = await self.worlds.create(await self.create_input(scan, body),
                                                 seed=seed, job_type="import",
                                                 copy_game=copy_game if game else None)
        with session_scope() as s:
            s.add(WorldImport(
                world_id=world.id, job_id=job_id, source_kind=source["kind"],
                source_label=label, container_id=source.get("container_id"),
                image=source.get("image", ""), volumes=scan.get("volumes") or [],
                bind_sources=scan.get("bind_sources") or [], state="importing"))
        self._pending[job_id] = (world.id, token)
        self.events.record(world.id, "imported", source=label)
        return world, job_id

    async def _copy_game_files(self, ctx: JobContext, new: World, source: dict[str, Any],
                               install: str, save: str | None) -> None:
        """Copy the old server's game files (not its saves, logs, backups or config) into
        the new world. The old server keeps running meanwhile: game files only change
        when it updates. The image's updater checks them against Steam on the first
        start, so anything missing or different is downloaded then."""
        dest = self.worlds.paths(new).server
        usage = shutil.disk_usage(dest)
        if usage.free < GAME_DOWNLOAD + 1024**3:
            raise JobFailed(f"Not enough free disk space to copy the game files "
                            f"({usage.free // 1024**3} GB free, about "
                            f"{GAME_DOWNLOAD // 1024**3} GB needed)")
        skip = set(NOT_GAME_FILES)
        save_name = ""
        if save and posixpath.dirname(save) == install:
            save_name = posixpath.basename(save)
            skip.add(save_name)
        label = source["label"]
        copied = 0

        def count(n: int) -> None:
            nonlocal copied
            copied += n

        async def run() -> None:
            if source["kind"] == "container":
                await self.docker.copy_tree_out(source["container_id"], install, dest,
                                                skip_top=skip, on_bytes=count)
                return
            res = await self.docker.run_helper(
                self.settings.helper_image,
                ["sh", "-c", GAME_COPY_SCRIPT, "sh", HOST_ROOT + install, save_name],
                binds={"/": f"{HOST_ROOT}:ro", str(dest): "/out"},
                timeout=GAME_COPY_TIMEOUT_S)
            if res.exit_code != 0:
                raise JobFailed(f"Could not copy the game files: {res.output.strip()}")

        ctx.progress(1, f"Copying game files from {label}")
        task = asyncio.create_task(run())
        while not task.done():
            await asyncio.wait({task}, timeout=2)
            if source["kind"] != "container":
                copied = await asyncio.to_thread(_tree_size, dest)
            ctx.progress(1 + min(17, int(copied * 18 / GAME_DOWNLOAD)),
                         f"Copying game files from {label}: {copied / 1024**3:.1f} GB")
        try:
            await task
        except DockerError as e:
            raise JobFailed(f"Could not copy the game files: {e}") from e
        if source["kind"] != "container":
            copied = await asyncio.to_thread(_tree_size, dest)
        ctx.log(f"Copied {copied // 1024**2} MB of game files from {label}; the first "
                "start checks them against Steam")

    async def _stop_source(self, ctx: JobContext, new: World, source: dict[str, Any]) -> None:
        c = await self._container(source)
        policy = c.restart_policy or "no"
        self._set(new.id, prev_restart_policy=policy, was_running=c.running)
        if policy != "no":
            # Before stopping: with "always", Docker would start it again.
            await self.docker.update_restart_policy(c.id, "no")
            ctx.log(f"Set {c.name}'s restart policy to 'no' (was '{policy}')")
        if c.running:
            ctx.progress(1, f"Stopping {c.name} (it saves on shutdown)")
            try:
                await self.docker.stop(c.id, STOP_TIMEOUT_S)
            except DockerError as e:
                raise JobFailed(f"Could not stop {c.name}: {e}") from e
            ctx.log(f"Stopped {c.name}")

    def _copy_bans(self, ctx: JobContext, new: World, doc: dict[str, Any]) -> None:
        """Bans aren't managed settings: they go straight into the new world's file."""
        bans = {k: doc[k] for k in ("bannedAccounts", "bans") if isinstance(doc.get(k), list)}
        if not any(bans.values()):
            return
        live = self.worlds.config.read_live(new)
        live.update(bans)
        atomic_write(self.worlds.paths(new).config, dumps_config(live), uid=new.puid,
                     gid=new.pgid)
        ctx.log(f"Copied {sum(len(v) for v in bans.values())} ban(s)")

    def _set(self, world_id: str, **fields: Any) -> None:
        with session_scope() as s:
            row = s.get(WorldImport, world_id)
            if row is not None:
                for k, v in fields.items():
                    setattr(row, k, v)

    def _on_job(self, job: dict[str, Any]) -> None:
        pending = self._pending.pop(job["id"], None)
        if pending is None:
            return
        world_id, token = pending
        if job["status"] == "succeeded":
            self._set(world_id, state="imported")
            # The files are in the new world now (and kept as its imported backup).
            shutil.rmtree(self._dir(token), ignore_errors=True)
            return
        task = asyncio.get_running_loop().create_task(self._rollback(world_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def recover_interrupted(self) -> None:
        """Imports cut off by a controller restart failed: put their old servers back."""
        with session_scope() as s:
            ids = [r.world_id for r in s.scalars(
                select(WorldImport).where(WorldImport.state == "importing"))]
        for world_id in ids:
            await self._rollback(world_id)

    async def _rollback(self, world_id: str) -> None:
        """A failed import: give the old server its port and restart policy back."""
        with session_scope() as s:
            row = s.get(WorldImport, world_id)
            if row is None:
                return
            s.expunge(row)
        try:
            new = self.worlds.get(world_id)
            name = container_name(new.slug)
            info = await self.docker.get_container(name)
            if info is not None and info.running:
                await self.docker.stop(name, new.stop_timeout_s)
            self.worlds._update(world_id, desired_state="stopped")
        except (WorldError, DockerError) as e:
            log.warning("import rollback: could not stop the new world: %s", e)
        if row.container_id and row.prev_restart_policy is not None:
            try:
                if row.prev_restart_policy != "no":
                    await self.docker.update_restart_policy(row.container_id,
                                                            row.prev_restart_policy)
                if row.was_running:
                    await self.docker.start(row.container_id)
            except DockerError as e:
                log.warning("import rollback: could not restart %s: %s", row.source_label, e)
        self._set(world_id, state="rolled_back")
        self.events.record(world_id, "import_rolled_back", source=row.source_label)

    # -- the old server afterwards ------------------------------------------------------

    def _row(self, world: World) -> WorldImport | None:
        with session_scope() as s:
            row = s.get(WorldImport, world.id)
            if row is not None:
                s.expunge(row)
            return row

    async def info(self, world: World) -> dict[str, Any] | None:
        row = self._row(world)
        if row is None:
            return None
        container = None
        if row.container_id and row.state not in ("container_removed", "volumes_removed"):
            c = await self.docker.get_container(row.container_id)
            container = None if c is None else {"name": c.name, "status": c.status,
                                                  "running": c.running,
                                                  "restart_policy": c.restart_policy}
        return {
            "state": row.state,
            "source_kind": row.source_kind,
            "source_label": row.source_label,
            "image": row.image,
            "volumes": row.volumes,
            "bind_sources": row.bind_sources,
            "container": container,
            "created_at": row.created_at.isoformat(),
        }

    async def remove_container(self, world: World) -> dict[str, Any]:
        row = self._row(world)
        if row is None or not row.container_id:
            raise WorldError("This world wasn't imported from a container", 404)
        if row.state != "imported":
            raise WorldError("Only an old server whose import succeeded can be removed", 409)
        c = await self.docker.get_container(row.container_id)
        if c is not None:
            if c.running:
                raise WorldError(f"{c.name} is running again; stop it first", 409)
            await self.docker.remove(c.id)
        self._set(world.id, state="container_removed")
        self.events.record(world.id, "import_cleanup", container=row.source_label)
        return await self.info(world) or {}

    async def remove_volumes(self, world: World) -> dict[str, Any]:
        row = self._row(world)
        if row is None or not row.container_id:
            raise WorldError("This world wasn't imported from a container", 404)
        if row.state != "container_removed":
            raise WorldError("Remove the old container first", 409)
        removed, kept = [], []
        for name in row.volumes:
            users = await self.docker.volume_users(name)
            if users:
                kept.append({"name": name, "used_by": users})
                continue
            try:
                await self.docker.remove_volume(name)
                removed.append(name)
            except DockerError as e:
                kept.append({"name": name, "used_by": [], "error": str(e)})
        if not kept:
            self._set(world.id, state="volumes_removed")
        self.events.record(world.id, "import_cleanup", volumes=removed)
        return {"removed": removed, "kept": kept, "import": await self.info(world)}

    async def dismiss(self, world: World) -> None:
        if self._row(world) is None:
            raise WorldError("This world wasn't imported", 404)
        self._set(world.id, state="dismissed")



def _tree_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total
