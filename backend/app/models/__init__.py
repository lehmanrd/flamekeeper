"""Database tables."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class World(Base):
    __tablename__ = "worlds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    image_repo: Mapped[str] = mapped_column(String(200), default="mornedhels/enshrouded-server")
    image_tag: Mapped[str] = mapped_column(String(100), default="latest")
    query_port: Mapped[int] = mapped_column(Integer, unique=True)
    bind_ip: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    puid: Mapped[int] = mapped_column(Integer, default=4711)
    pgid: Mapped[int] = mapped_column(Integer, default=4711)
    game_branch: Mapped[str] = mapped_column(String(64), default="public")
    steamcmd_args: Mapped[str] = mapped_column(String(200), default="validate")
    use_ntsync: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_timeout_s: Mapped[int] = mapped_column(Integer, default=90)
    cpu_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delegate_schedules: Mapped[bool] = mapped_column(Boolean, default=False)
    # Updates: manual | immediate | when_empty | window
    update_policy: Mapped[str] = mapped_column(String(16), default="when_empty")
    update_window_cron: Mapped[str] = mapped_column(String(100), default="0 4 * * *")
    # notify | when_empty
    image_update_policy: Mapped[str] = mapped_column(String(16), default="notify")
    pre_update_backup: Mapped[bool] = mapped_column(Boolean, default=True)
    # A build whose automatic update failed: not retried automatically.
    auto_update_failed_build: Mapped[str | None] = mapped_column(String(32), nullable=True)
    desired_state: Mapped[str] = mapped_column(String(16), default="stopped")
    config_revision: Mapped[int] = mapped_column(Integer, default=0)
    applied_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Schedule(Base):
    __tablename__ = "schedules"

    world_id: Mapped[str] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    cron: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    only_when_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_count: Mapped[int] = mapped_column(Integer, default=0)
    # Backups: skip a scheduled run when nobody played since the last one.
    skip_if_idle: Mapped[bool] = mapped_column(Boolean, default=True)
    # Backups: delete non-scheduled backups (manual, pre-*, uploaded) older than this
    # many days; 0 keeps them until deleted by hand.
    max_age_days: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    game_build_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When the server wrote the savegame in this backup. A running server only saves
    # every few minutes, so this can be earlier than created_at.
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    world_id: Mapped[str | None] = mapped_column(
        ForeignKey("worlds.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PlayerSession(Base):
    __tablename__ = "player_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    world_id: Mapped[str] = mapped_column(ForeignKey("worlds.id", ondelete="CASCADE"), index=True)
    player_name: Mapped[str] = mapped_column(String(200))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetricSample(Base):
    __tablename__ = "metric_samples"

    world_id: Mapped[str] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_tx_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    players: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AppSetting(Base):
    """A global setting changed in the UI (overrides its environment default)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
