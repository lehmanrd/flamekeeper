"""Request bodies for the REST API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorldCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=40)
    slot_count: int = Field(default=16, ge=1, le=16)
    query_port: int | None = Field(default=None, ge=1024, le=65535)
    image_tag: str | None = Field(default=None, max_length=100)
    preset: Literal["Default", "Relaxed", "Hard", "Survival", "Custom"] = "Default"
    # Wizard
    roles: list[dict[str, Any]] | None = Field(
        default=None, description="Roles with passwords; omitted = the four default roles")
    game_branch: str | None = Field(default=None, max_length=64)
    update_policy: Literal["manual", "immediate", "when_empty", "window"] | None = None
    backup: BackupPlan | None = None
    start: bool = True
    copy_game_from: str | None = Field(
        default=None, description="World id or slug to copy game files from")


class BackupPlan(BaseModel):
    enabled: bool = True
    cron: str = Field(default="0 * * * *", max_length=100)
    retention_count: int = Field(default=48, ge=0, le=10_000)


class StopRequest(BaseModel):
    when: Literal["now", "empty"] = "now"


class RestartRequest(BaseModel):
    mode: Literal["process", "container"] = "process"
    when: Literal["now", "empty"] = "now"


class JobRef(BaseModel):
    job_id: str


class ConfigPut(BaseModel):
    """A partial config edit; omitted sections are left unchanged."""

    base_revision: int | None = Field(
        default=None, description="Revision the edit is based on; 409 if it is stale")
    preset: str | None = None
    gameSettings: dict[str, Any] | None = None  # noqa: N815 - JSON key in the game file
    server: dict[str, Any] | None = None
    roles: list[dict[str, Any]] | None = Field(
        default=None,
        description="Each role: name, permissions, reservedSlots, and either `password` "
                    "or `keep_password_from` (index of a role in the current file)")
    note: str = Field(default="", max_length=200)


class ConfigImport(BaseModel):
    document: Any
    base_revision: int | None = None
    convert_legacy_password: bool = False


class BaseRevision(BaseModel):
    base_revision: int | None = None


class ApplyRequest(BaseModel):
    when: Literal["now", "empty"] = "now"


class InfraPatch(BaseModel):
    """Container settings (Server tab). Every field recreates the container."""

    model_config = ConfigDict(extra="forbid")

    image_tag: str | None = Field(default=None, max_length=128)
    query_port: int | None = None
    bind_ip: str | None = None
    puid: int | None = None
    pgid: int | None = None
    game_branch: str | None = None
    steamcmd_args: str | None = None
    use_ntsync: bool | None = None
    stop_timeout_s: int | None = None
    cpu_limit: float | None = None
    mem_limit_mb: int | None = None


class BackupCreate(BaseModel):
    note: str = Field(default="", max_length=500)
    # For a running server: wait for its next autosave (default), take the save that's
    # on disk, or stop it so it saves everything (and start it again).
    mode: Literal["next_save", "last_save", "stop"] = "next_save"


class BackupNote(BaseModel):
    note: str = Field(max_length=500)


class RestoreRequest(BaseModel):
    restore_config: bool = False
    when: Literal["now", "empty"] = "now"


class CloneRequest(BaseModel):
    source: str = Field(default="current", description='"current" or a backup id')
    display_name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=40)
    query_port: int | None = Field(default=None, ge=1024, le=65535)


class BackupScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    skip_if_idle: bool | None = None
    retention_count: int | None = None
    max_age_days: int | None = None


class UpdateRequest(BaseModel):
    target: Literal["game", "image"] = "game"
    when: Literal["now", "empty"] = "now"
    force: bool = False


class UpdatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    update_policy: Literal["manual", "immediate", "when_empty", "window"] | None = None
    update_window_cron: str | None = Field(default=None, max_length=100)
    image_update_policy: Literal["notify", "when_empty"] | None = None
    pre_update_backup: bool | None = None
