"""Controller settings, read from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "FlameKeeper"
APP_SLUG = "flamekeeper"
SCHEMA_VERSION = 1

LABEL_MANAGED = f"{APP_SLUG}.managed"
LABEL_WORLD_ID = f"{APP_SLUG}.world_id"
LABEL_VERSION = f"{APP_SLUG}.version"
# Fingerprint of the infra settings the container was created with (detects a needed recreate).
LABEL_INFRA = f"{APP_SLUG}.infra"

GAME_IMAGE_REPO = "mornedhels/enshrouded-server"
GAME_MOUNT = "/opt/enshrouded"
# Paths inside the game volume (see docs/game-server-behavior.md).
GAME_SERVER_SUBDIR = "server"
SERVER_CONFIG_NAME = "enshrouded_server.json"
STEAM_APP_ID = 2278520


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    data_dir: Path = Path("/srv/flamekeeper")
    base_path: str = "/"
    log_level: str = "INFO"
    database_url: str | None = None

    # Wine, not Proton ("latest"): the Proton build crash-loops on game version 1.0.
    default_image_tag: str = "stable-wine"
    default_puid: int = 4711
    default_pgid: int = 4711
    first_query_port: int = 15637

    # Docker network shared by the controller and game containers. A2S queries go
    # to the game container by name on this network when the controller runs in
    # a container; otherwise they go to a2s_host.
    docker_network: str = "flamekeeper"
    a2s_mode: str = "auto"  # auto | network | host
    a2s_host: str = "127.0.0.1"

    helper_image: str = "busybox:stable"

    # Updates. The latest game build comes from Steam app info, by running
    # steamcmd in a short-lived container; api.steamcmd.net (what the image's own updater
    # uses) can be the source or a fallback.
    update_check_interval_min: int = 30
    steam_info_source: str = "steamcmd"  # steamcmd | api
    steam_api_fallback: bool = False
    steamcmd_image: str = "steamcmd/steamcmd:latest"
    image_check_interval_hours: int = 24
    # FlameKeeper's own releases (app.services.app_update). FLAMEKEEPER_VERSION is set in
    # the image at build time; FLAMEKEEPER_CLI by the installer's compose file.
    flamekeeper_version: str = "dev"
    flamekeeper_repo: str = "lehmanrd/flamekeeper"
    flamekeeper_cli: str = ""
    check_for_app_updates: bool = True
    metrics_retention_days: int = 7
    # Skip the DATA_DIR path-match check (development only).
    skip_path_check: bool = False
    # Use the in-memory fake Docker adapter (frontend development, demos).
    fake_docker: bool = False
    static_dir: Path | None = None

    @field_validator("base_path")
    @classmethod
    def _normalize_base_path(cls, v: str) -> str:
        v = "/" + v.strip("/")
        return v if v != "/" else ""

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'flamekeeper.db'}"

    @property
    def worlds_dir(self) -> Path:
        return self.data_dir / "worlds"

    @property
    def in_container(self) -> bool:
        return Path("/.dockerenv").exists()

    @property
    def resolved_a2s_mode(self) -> str:
        if self.a2s_mode != "auto":
            return self.a2s_mode
        return "network" if self.in_container else "host"

    @property
    def resolved_static_dir(self) -> Path | None:
        if self.static_dir:
            return self.static_dir
        candidate = Path(__file__).resolve().parent / "static"
        return candidate if candidate.exists() else None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_root() -> bool:
    return os.geteuid() == 0
