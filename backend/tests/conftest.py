from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import init_engine, run_migrations
from app.main import create_app
from app.services.docker_fake import FakeDockerAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        fake_docker=True,
        skip_path_check=True,
        default_puid=1000,
        default_pgid=1000,
        static_dir=tmp_path / "no-static",
    )


@pytest.fixture
def db(settings: Settings) -> Settings:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    run_migrations(settings.db_url)
    init_engine(settings.db_url)
    return settings


@pytest.fixture
def fake_docker() -> FakeDockerAdapter:
    return FakeDockerAdapter()


@pytest.fixture
def client(settings: Settings, fake_docker: FakeDockerAdapter) -> Iterator[TestClient]:
    app = create_app(settings, docker=fake_docker, poll_interval=0.05)
    with TestClient(app) as c:
        yield c


def wait_job(client: TestClient, job_id: str, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def wait_for(predicate, timeout: float = 5, interval: float = 0.05):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met; last value: {last!r}")
