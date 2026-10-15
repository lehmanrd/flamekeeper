"""Backup zip files: writing, inspecting and safely extracting them.

Layout written by FlameKeeper:

    flamekeeper-backup.json     manifest (world, build, kind, note, config revision, files)
    enshrouded_server.json      the world's config, passwords included
    savegame/<files>            the server's savegame folder

Uploaded or image-made zips may instead hold the save files at the top level or in some
other folder; `inspect_zip` finds them by name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "flamekeeper-backup.json"
CONFIG_NAME = "enshrouded_server.json"
SAVE_DIR = "savegame"
FORMAT_VERSION = 1
# Limits for zips we didn't write (uploads): protects the disk from zip bombs.
MAX_UNCOMPRESSED_BYTES = 50 * 1024**3
MAX_ENTRIES = 10_000
MAX_RATIO = 200

# Enshrouded save files: a world slot (8 hex chars), its rolling versions (-1 … -9) and
# its index, e.g. 3ad85aea, 3ad85aea-4, 3ad85aea-index. See docs/game-server-behavior.md.
SAVE_FILE_RE = re.compile(r"^[0-9a-f]{8}(?:-\d+|-index|_info)?$")


class ArchiveError(Exception):
    pass


@dataclass
class ZipSummary:
    save_prefix: str  # folder inside the zip holding the save files ("" = top level)
    save_files: list[str]
    has_config: bool
    manifest: dict[str, Any] | None
    uncompressed_bytes: int
    entries: int = 0
    warnings: list[str] = field(default_factory=list)


def latest_mtime(folder: Path) -> datetime | None:
    """When the newest file in `folder` was written (the server's last save)."""
    newest = None
    if folder.is_dir():
        for p in folder.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                newest = m if newest is None or m > newest else newest
    return datetime.fromtimestamp(newest, UTC) if newest is not None else None


def snapshot_dir(src: Path, dest: Path, attempts: int = 3) -> list[Path]:
    """Copy `src` into `dest` so that the copy is consistent while the server runs.

    The server writes a new save roughly every 10 minutes. A file set that changes
    while it's being copied is copied again (up to `attempts` times).
    """

    def state() -> dict[str, tuple[int, int]]:
        out = {}
        for p in sorted(src.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(src))] = (st.st_size, st.st_mtime_ns)
        return out

    for _ in range(attempts):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        before = state()
        for rel in before:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src / rel, target)
            except FileNotFoundError:
                break  # a rolling save was replaced mid-copy; retry
        else:
            if state() == before:
                return [dest / rel for rel in before]
    raise ArchiveError("The savegame kept changing while it was copied; try again, or "
                       "stop the server for a consistent backup")


def write_backup(dest: Path, save_dir: Path, config: bytes | None,
                 manifest: dict[str, Any]) -> int:
    """Write the zip atomically (temp file + rename). Returns its size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in save_dir.rglob("*") if p.is_file())
    manifest = {**manifest, "format": FORMAT_VERSION,
                "files": [{"name": f"{SAVE_DIR}/{p.relative_to(save_dir).as_posix()}",
                           "size": p.stat().st_size} for p in files]}
    fd, tmp = tempfile.mkstemp(prefix=".backup-", suffix=".zip.tmp", dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            if config is not None:
                z.writestr(CONFIG_NAME, config)
            for p in files:
                z.write(p, f"{SAVE_DIR}/{p.relative_to(save_dir).as_posix()}")
        os.chmod(tmp, 0o600)  # the config inside holds role passwords
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest.stat().st_size


def _safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return not (name.startswith(("/", "\\")) or ".." in p.parts or ":" in name
                or "\\" in name)


def inspect_zip(path: Path, *, trusted: bool = False) -> ZipSummary:
    """Validate a backup zip and find its save files. Raises ArchiveError."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ArchiveError(f"Not a valid zip file: {e}") from e
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(f"The zip has {len(infos)} entries; at most {MAX_ENTRIES}")
        total = 0
        saves: dict[str, list[str]] = {}
        manifest = None
        has_config = False
        for info in infos:
            if not _safe_name(info.filename):
                raise ArchiveError(f"Unsafe path in zip: {info.filename!r}")
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ArchiveError(f"The zip contains a symlink: {info.filename!r}")
            total += info.file_size
            ratio = info.file_size / max(info.compress_size, 1)
            if not trusted and info.file_size > 1024**2 and ratio > MAX_RATIO:
                raise ArchiveError(f"Suspicious compression ratio for {info.filename!r}")
            p = PurePosixPath(info.filename)
            if p.name == MANIFEST_NAME and len(p.parts) == 1:
                try:
                    manifest = json.loads(z.read(info))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    manifest = None
            elif p.name == CONFIG_NAME and len(p.parts) == 1:
                has_config = True
            elif SAVE_FILE_RE.match(p.name):
                saves.setdefault(str(p.parent) if str(p.parent) != "." else "", []).append(
                    info.filename)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ArchiveError("The zip is too large once extracted")
        if not saves:
            raise ArchiveError("No Enshrouded save files found (expected files like "
                               "3ad85aea and 3ad85aea-index)")
        if len(saves) > 1:
            if SAVE_DIR in saves:
                prefix = SAVE_DIR
            else:
                raise ArchiveError("Save files were found in more than one folder: "
                                   + ", ".join(sorted(p or "(top level)" for p in saves)))
        else:
            prefix = next(iter(saves))
        summary = ZipSummary(prefix, sorted(saves[prefix]), has_config, manifest, total,
                             len(infos))
        if not any(f.endswith("-index") for f in summary.save_files):
            summary.warnings.append("No -index file: the server may not find the latest save")
        return summary


def extract_saves(path: Path, summary: ZipSummary, savegame: Path) -> list[Path]:
    """Replace the contents of `savegame` with the save folder from the zip.

    Everything under the save prefix is extracted (not only files matching the save
    name pattern), so nothing the game keeps there is lost.
    """
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=savegame.parent))
    try:
        prefix = PurePosixPath(summary.save_prefix) if summary.save_prefix else None
        written = []
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or not _safe_name(info.filename):
                    continue
                p = PurePosixPath(info.filename)
                if prefix is not None:
                    if p.parts[: len(prefix.parts)] != prefix.parts:
                        continue
                    rel = PurePosixPath(*p.parts[len(prefix.parts):])
                else:
                    if p.name in (MANIFEST_NAME, CONFIG_NAME) and len(p.parts) == 1:
                        continue
                    rel = p
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(rel)
        # mkdtemp makes the folder 0700; keep the permissions the savegame folder had.
        mode = savegame.stat().st_mode & 0o7777 if savegame.exists() else 0o755
        os.chmod(staging, mode)
        old = savegame.with_name(savegame.name + ".old")
        if old.exists():
            shutil.rmtree(old)
        if savegame.exists():
            savegame.rename(old)
        try:
            staging.rename(savegame)
        except BaseException:
            if old.exists() and not savegame.exists():
                old.rename(savegame)  # put the previous saves back
            raise
        shutil.rmtree(old, ignore_errors=True)
        return [savegame / r for r in written]
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def read_config(path: Path) -> dict[str, Any] | None:
    with zipfile.ZipFile(path) as z:
        try:
            return json.loads(z.read(CONFIG_NAME))
        except KeyError:
            return None
