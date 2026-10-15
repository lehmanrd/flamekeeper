"""On-disk layout and naming for a world."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import APP_SLUG, GAME_SERVER_SUBDIR, SERVER_CONFIG_NAME, STEAM_APP_ID

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_BUILDID_RE = re.compile(r'"buildid"\s+"(\d+)"')


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:40].strip("-") or "world"


def container_name(slug: str) -> str:
    return f"{APP_SLUG}-{slug}"


@dataclass(frozen=True)
class WorldPaths:
    root: Path

    @classmethod
    def for_slug(cls, worlds_dir: Path, slug: str) -> WorldPaths:
        return cls(worlds_dir / slug)

    @property
    def game(self) -> Path:
        """Mounted at /opt/enshrouded in the game container."""
        return self.root / "game"

    @property
    def server(self) -> Path:
        return self.game / GAME_SERVER_SUBDIR

    @property
    def config(self) -> Path:
        return self.server / SERVER_CONFIG_NAME

    @property
    def savegame(self) -> Path:
        return self.server / "savegame"

    @property
    def game_logs(self) -> Path:
        return self.server / "logs"

    @property
    def appmanifest(self) -> Path:
        return self.server / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"

    @property
    def current_version(self) -> Path:
        """Written by the image's updater (/opt/enshrouded/current_version)."""
        return self.game / "current_version"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def image_backups(self) -> Path:
        """Where the image's own backup program writes (BACKUP_DIR=backups, relative to
        the server install)."""
        return self.server / "backups"

    @property
    def history(self) -> Path:
        return self.root / "config-history"


def parse_buildid(acf_text: str) -> str | None:
    """Build id from an app manifest; None while not installed (steamcmd writes 0)."""
    m = _BUILDID_RE.search(acf_text)
    return m.group(1) if m and m.group(1) != "0" else None


def installed_build(paths: WorldPaths) -> str | None:
    """The installed game build, as the image's updater sees it.

    The image writes /opt/enshrouded/current_version after each successful update and
    decides whether to update by comparing it with Steam; it can be missing (e.g. right
    after a force update), so the Steam app manifest is the fallback. steamcmd doesn't
    rewrite the manifest's build id when the files are already current.
    """
    try:
        version = paths.current_version.read_text().strip()
        if version.isdigit() and version != "0":
            return version
    except OSError:
        pass
    try:
        return parse_buildid(paths.appmanifest.read_text(errors="replace"))
    except OSError:
        return None
