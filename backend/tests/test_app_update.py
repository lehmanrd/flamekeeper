"""FlameKeeper's own updates: the release check and updating from the UI."""

from __future__ import annotations

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.app_update import HELPER_NAME, parse_version
from app.services.docker_adapter import ContainerInfo
from app.services.docker_fake import FakeDockerAdapter
from app.services.notifications import EVENTS
from tests.conftest import admin_client, wait_for

RELEASE = {"tag_name": "v0.5.0", "name": "FlameKeeper 0.5.0",
           "html_url": "https://github.com/lehmanrd/flamekeeper/releases/tag/v0.5.0",
           "published_at": "2026-10-16T10:00:00Z"}
OWN_IMAGE = "ghcr.io/lehmanrd/flamekeeper:0.4.1"


class Calls:
    def __init__(self, release: dict | Exception = RELEASE) -> None:
        self.release, self.count, self.app_events = release, 0, []

    async def fetch(self, repo: str) -> dict:
        assert repo == "lehmanrd/flamekeeper"
        self.count += 1
        if isinstance(self.release, Exception):
            raise self.release
        return self.release


def _setup(c: TestClient, settings: Settings, fake: FakeDockerAdapter, *, version: str = "0.4.1",
           cli: str = "/usr/local/bin/flamekeeper") -> Calls:
    settings.flamekeeper_version, settings.flamekeeper_cli = version, cli
    calls = Calls()
    updater = c.app.state.services.app_update
    updater.fetch = calls.fetch
    updater.notifier.on_app_event = lambda kind, data: calls.app_events.append((kind, data))
    own = socket.gethostname()
    if not fake._find(own):
        fake.containers["own"] = ContainerInfo(id="own", name=own, image=OWN_IMAGE, labels={},
                                               status="running")
    return calls


def test_parse_version() -> None:
    assert parse_version("v0.10.2") == (0, 10, 2) > parse_version("0.9.9")
    assert parse_version("dev") is None and parse_version("1.0") is None


def test_check_finds_a_newer_release_and_notifies_once(
        client: TestClient, settings: Settings, fake_docker: FakeDockerAdapter) -> None:
    calls = _setup(client, settings, fake_docker)
    st = client.get("/api/system/update").json()
    assert st["latest"] is None and not st["update_available"] and st["can_update"]

    st = client.post("/api/system/update/check").json()
    assert st["update_available"] and st["latest"]["version"] == "0.5.0"
    assert st["latest"]["url"].endswith("/v0.5.0") and st["checked_at"]
    assert calls.app_events == [("app_update_available", {
        "version": "0.5.0", "current": "0.4.1", "url": RELEASE["html_url"]})]

    # "Check now" twice in a row asks GitHub once; a later check doesn't notify again.
    client.post("/api/system/update/check")
    assert calls.count == 1
    client.app.state.services.app_update._last_manual -= 60
    client.post("/api/system/update/check")
    assert calls.count == 2 and len(calls.app_events) == 1

    calls.release = httpx.ConnectError("no network")
    client.app.state.services.app_update._last_manual -= 60
    st = client.post("/api/system/update/check").json()
    assert "no network" in st["check_error"] and st["latest"]["version"] == "0.5.0"


def test_who_can_update(client: TestClient, settings: Settings,
                        fake_docker: FakeDockerAdapter) -> None:
    _setup(client, settings, fake_docker, version="dev")
    client.post("/api/system/update/check")
    st = client.get("/api/system/update").json()
    assert not st["update_available"] and "development build" in st["cannot_update_reason"]

    settings.flamekeeper_version, settings.flamekeeper_cli = "0.4.1", ""
    st = client.get("/api/system/update").json()
    assert st["update_available"] and not st["can_update"]
    assert "installer" in st["cannot_update_reason"] and st["command"] == "sudo flamekeeper update"
    res = client.post("/api/system/update", json={"version": "0.5.0"})
    assert res.status_code == 409 and "installer" in res.json()["detail"]

    settings.flamekeeper_version = "0.5.0"  # already on the latest
    assert not client.get("/api/system/update").json()["update_available"]


def test_update_runs_the_host_cli_and_reports_a_failure(
        client: TestClient, settings: Settings, fake_docker: FakeDockerAdapter) -> None:
    calls = _setup(client, settings, fake_docker)
    client.post("/api/system/update/check")
    assert client.post("/api/system/update", json={"version": "0.4.9"}).status_code == 409

    res = client.post("/api/system/update", json={"version": "0.5.0"})
    assert res.status_code == 202, res.text
    run = res.json()["run"]
    assert run["status"] == "running" and (run["from"], run["to"]) == ("0.4.1", "0.5.0")
    [(name, image, cmd, privileged, pid_host)] = fake_docker.detached
    assert (name, image, privileged, pid_host) == (HELPER_NAME, OWN_IMAGE, True, True)
    assert cmd[:3] == ["nsenter", "-t", "1"]
    log = settings.data_dir / "self-update" / "update.log"
    assert cmd[-1] == (f"/usr/local/bin/flamekeeper update --version 0.5.0 --no-firewall "
                       f">{log} 2>&1")
    assert client.post("/api/system/update", json={"version": "0.5.0"}).status_code == 409

    log.write_text("==> Starting FlameKeeper\n\x1b[31mflamekeeper: download failed\x1b[0m\n")
    fake_docker.finish_detached(HELPER_NAME, 1)
    run = wait_for(lambda: (r := client.get("/api/system/update").json()["run"])["status"]
                   != "running" and r)
    assert run["status"] == "failed" and "download failed" in run["error"]
    assert run["log_tail"].endswith("flamekeeper: download failed")
    assert fake_docker._find(HELPER_NAME) is None
    assert calls.app_events[-1][0] == "app_updated" and not calls.app_events[-1][1]["ok"]


def test_the_new_controller_finishes_the_update(settings: Settings,
                                               fake_docker: FakeDockerAdapter) -> None:
    """The old controller starts the helper and is replaced; the new one records the result."""
    with admin_client(create_app(settings, docker=fake_docker, poll_interval=0.05),
                      settings) as old:
        _setup(old, settings, fake_docker)
        old.post("/api/system/update/check")
        assert old.post("/api/system/update", json={"version": "0.5.0"}).status_code == 202

    settings.flamekeeper_version = "0.5.0"
    with admin_client(create_app(settings, docker=fake_docker, poll_interval=0.05),
                      settings) as new:
        calls = _setup(new, settings, fake_docker, version="0.5.0")
        assert new.get("/api/system/update").json()["run"]["status"] == "running"
        fake_docker.finish_detached(HELPER_NAME, 0)
        run = wait_for(lambda: (r := new.get("/api/system/update").json()["run"])["status"]
                       != "running" and r)
        assert run["status"] == "succeeded" and run["error"] is None
        assert calls.app_events == [("app_updated", {"ok": True, "from": "0.4.1",
                                                     "to": "0.5.0", "error": None})]
        assert not new.get("/api/system/update").json()["update_available"]


@pytest.mark.parametrize("seen,expected", [(None, True), (list(EVENTS), False)])
def test_new_notification_events_start_on(settings: Settings, fake_docker: FakeDockerAdapter,
                                          seen: list | None, expected: bool) -> None:
    """Notification settings saved before 0.5 get the new FlameKeeper events turned on;
    settings saved since keep the admin's choice."""
    from app.db import session_scope
    from app.models import AppSetting

    with admin_client(create_app(settings, docker=fake_docker), settings) as c:
        with session_scope() as s:
            value = {"discord_webhook": "", "events": ["online"]}
            s.merge(AppSetting(key="notifications",
                               value={**value, **({"seen": seen} if seen else {})}))
        c.app.state.services.notifier.load()
        events = c.get("/api/settings/notifications").json()["events"]
        assert ("app_update_available" in events) is expected and "online" in events
