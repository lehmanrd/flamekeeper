"""Service wiring: one instance of each service per app."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.services.app_settings import AppSettings
from app.services.backup_service import BackupService
from app.services.bus import Bus
from app.services.config_service import ConfigService
from app.services.docker_adapter import DockerAdapter
from app.services.events import EventLog
from app.services.jobs import JobManager
from app.services.metrics import MetricsService
from app.services.monitor import Monitor
from app.services.scheduler import Scheduler
from app.services.selfcheck import SelfCheckResult
from app.services.update_service import UpdateService
from app.services.world_service import WorldService


@dataclass
class Services:
    settings: Settings
    docker: DockerAdapter
    bus: Bus
    jobs: JobManager
    events: EventLog
    config: ConfigService
    worlds: WorldService
    monitor: Monitor
    backups: BackupService
    scheduler: Scheduler
    updates: UpdateService
    metrics: MetricsService
    app_settings: AppSettings
    selfcheck: SelfCheckResult | None = None

    @classmethod
    def build(cls, settings: Settings, docker: DockerAdapter,
              poll_interval: float | None = None) -> Services:
        bus = Bus()
        jobs = JobManager(bus)
        events = EventLog(bus)
        config = ConfigService(settings.worlds_dir, events=events)
        worlds = WorldService(settings, docker, jobs, events, config)
        kwargs = {"poll_interval": poll_interval} if poll_interval else {}
        monitor = Monitor(settings, docker, bus, events, jobs, **kwargs)
        worlds.monitor = monitor
        monitor.recreate_check = worlds.recreate_required
        backups = BackupService(settings, worlds, jobs, events, config)
        worlds.on_world_created = backups.ensure_default_schedule
        updates = UpdateService(settings, docker, worlds, jobs, events, backups)
        metrics = MetricsService(docker, **({"sample_every": poll_interval}
                                            if poll_interval else {}),
                                 retention_days=lambda: settings.metrics_retention_days)
        app_settings = AppSettings(settings)
        monitor.metrics = metrics
        monitor.update_summary = updates.summary
        scheduler = Scheduler(worlds, backups, updates,
                              **({"tick": poll_interval} if poll_interval else {}))
        return cls(settings, docker, bus, jobs, events, config, worlds, monitor, backups,
                   scheduler, updates, metrics, app_settings)
