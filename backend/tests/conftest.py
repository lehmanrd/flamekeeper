from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
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


ADMIN = {"username": "admin", "password": "correct-horse-battery"}
CSRF = {"X-FlameKeeper": "1"}


@pytest.fixture
def anon(settings: Settings, fake_docker: FakeDockerAdapter) -> Iterator[TestClient]:
    """A client before first setup: no admin account, not signed in."""
    app = create_app(settings, docker=fake_docker, poll_interval=0.05)
    with TestClient(app, headers=CSRF) as c:
        # No background scheduler: its first pass can overlap a test and start an
        # automatic update or backup of its own. Tests run scheduled work explicitly.
        c.portal.call(c.app.state.services.scheduler.stop)
        yield c


def sign_in(c: TestClient, settings: Settings) -> TestClient:
    """Create the admin through first setup, or sign in if it already exists."""
    if c.get("/api/auth/status").json()["setup_required"]:
        res = c.post("/api/auth/setup", json=ADMIN)
    else:
        res = c.post("/api/auth/login", json=ADMIN)
    assert res.status_code == 200, res.text
    return c


@contextmanager
def admin_client(app, settings: Settings) -> Iterator[TestClient]:  # noqa: ANN001
    """A signed-in client for an app a test builds itself."""
    with TestClient(app, headers=CSRF) as c:
        yield sign_in(c, settings)


@pytest.fixture
def client(anon: TestClient, settings: Settings) -> TestClient:
    """Signed in as the admin created through first setup."""
    return sign_in(anon, settings)


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
