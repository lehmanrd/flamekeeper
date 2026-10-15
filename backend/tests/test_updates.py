"""Update detection and jobs, policies, metrics, player history."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import session_scope
from app.models import PlayerSession
from app.services import world_service
from app.services.docker_adapter import parse_stats
from app.services.docker_fake import FakeDockerAdapter
from app.services.update_service import buildid_from_app_info, parse_vdf
from tests.conftest import FIXTURES, wait_for, wait_job
from tests.test_lifecycle import _create


def _manifest_build(settings: Settings, slug: str = "embervale") -> str:
    acf = settings.worlds_dir / slug / "game" / "server" / "steamapps" / "appmanifest_2278520.acf"
    return acf.read_text().split('"buildid"')[1].split('"')[1]


def _services(client: TestClient):
    """The app's services; the client fixture has already stopped the background
    scheduler, so the test decides when policies run."""
    return client.app.state.services


def _status(client: TestClient, wid: str, check: bool = False) -> dict:
    if check:
        return client.post(f"/api/worlds/{wid}/update/check").json()
    return client.get(f"/api/worlds/{wid}/update").json()


# -- parsing ---------------------------------------------------------------------------


def test_parse_real_steamcmd_app_info() -> None:
    text = (FIXTURES / "steamcmd-app-info-2278520.txt").read_text()
    assert buildid_from_app_info(text, "public") == "23178631"
    assert buildid_from_app_info(text, "experimental") is None
    assert buildid_from_app_info("steamcmd banner, no app info", "public") is None
    assert parse_vdf('"a" { "b" "1" "c" { "d" "x \\" y" } }') == \
        {"a": {"b": "1", "c": {"d": 'x \\" y'}}}


def test_parse_stats_like_docker_cli() -> None:
    s = parse_stats({
        "cpu_stats": {"cpu_usage": {"total_usage": 3_000}, "system_cpu_usage": 20_000,
                      "online_cpus": 8},
        "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000},
        "memory_stats": {"usage": 900, "limit": 4000, "stats": {"inactive_file": 100}},
        "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20},
                     "eth1": {"rx_bytes": 1, "tx_bytes": 2}},
    })
    assert (s.cpu_pct, s.mem_bytes, s.mem_limit, s.net_rx, s.net_tx) == (160.0, 800, 4000, 11, 22)


# -- detection -------------------------------------------------------------------------


def test_update_detection(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    st = _status(client, wid, check=True)
    assert st["game"] == {**st["game"], "installed": "23178631", "latest": "23178631",
                          "source": "steamcmd", "update_available": False}
    assert st["policy"]["update_policy"] == "when_empty" and st["pending_job"] is None

    fake_docker.latest_build = "23200000"
    assert _status(client, wid)["game"]["latest"] == "23178631"  # cached
    st = _status(client, wid, check=True)
    assert st["game"]["update_available"] and st["game"]["latest"] == "23200000"
    summary = client.get("/api/worlds").json()[0]
    assert summary["game_update_available"] and summary["latest_build"] == "23200000"


def test_image_update_detection_and_job(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    image = "mornedhels/enshrouded-server:stable-wine"
    fake_docker.local_digests[image] = "sha256:old"
    fake_docker.registry_digests[image] = "sha256:new"
    st = _status(client, wid, check=True)
    assert st["image"]["update_available"]
    assert client.get("/api/worlds").json()[0]["image_update_available"]

    fake_docker.local_digests[image] = "sha256:new"  # what the pull brings in
    job = client.post(f"/api/worlds/{wid}/update", json={"target": "image"}).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded", client.get(f"/api/jobs/{job}").json()
    assert ("create", "flamekeeper-embervale") in fake_docker.calls[-4:]
    kinds = [b["kind"] for b in client.get(f"/api/worlds/{wid}/backups").json()["backups"]]
    assert "pre-image-update" in kinds
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    updated = [e for e in client.get(f"/api/worlds/{wid}/events").json() if e["kind"] == "updated"]
    assert updated[0]["data"]["target"] == "image"


# -- game update jobs ------------------------------------------------------------------


def test_manual_game_update(client: TestClient, settings: Settings,
                            fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    fake_docker.latest_build = "23200000"
    _status(client, wid, check=True)
    job = client.post(f"/api/worlds/{wid}/update", json={}).json()["job_id"]
    result = wait_job(client, job)
    assert result["status"] == "succeeded", client.get(f"/api/jobs/{job}").json()
    assert result["message"] == "Updated 23178631 → 23200000"
    assert _manifest_build(settings) == "23200000"
    calls = [c[1] for c in fake_docker.exec_calls if c[1][0] == "supervisorctl"][-2:]
    assert calls == [["supervisorctl", "stop", "enshrouded-server"],
                     ["supervisorctl", "start", "enshrouded-updater"]]
    log = client.get(f"/api/jobs/{job}").json()["log"]
    assert "pre-update" in log
    backups = client.get(f"/api/worlds/{wid}/backups").json()["backups"]
    assert backups[0]["kind"] == "pre-update"
    assert backups[0]["note"] == "Before update 23178631 → 23200000"
    st = _status(client, wid)
    assert not st["game"]["update_available"] and st["game"]["installed"] == "23200000"
    hist = client.get(f"/api/worlds/{wid}/update/history").json()
    assert hist[0]["status"] == "succeeded" and hist[0]["type"] == "update"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    assert client.post(f"/api/worlds/{wid}/update", json={"force": True}).status_code == 202


def test_update_of_a_stopped_world_leaves_it_stopped(client: TestClient, settings: Settings,
                                                    fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    wait_job(client, client.post(f"/api/worlds/{wid}/stop").json()["job_id"])
    fake_docker.latest_build = "23200000"
    _status(client, wid, check=True)
    job = client.post(f"/api/worlds/{wid}/update", json={}).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded", client.get(f"/api/jobs/{job}").json()
    assert _manifest_build(settings) == "23200000"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "stopped")


def test_failed_auto_update_is_not_retried(client: TestClient, settings: Settings,
                                           fake_docker: FakeDockerAdapter, monkeypatch) -> None:
    world = _create(client)
    wid = world["id"]
    client.put(f"/api/worlds/{wid}/update-policy", json={"update_policy": "immediate"})
    monkeypatch.setattr(fake_docker, "_write_manifest", lambda c, build: None)  # steamcmd fails
    fake_docker.latest_build = "23200000"
    services = _services(client)
    _status(client, wid, check=True)
    w = services.worlds.get(wid)
    jobs = client.portal.call(services.updates.tick)
    assert len(jobs) == 1
    job = wait_job(client, jobs[0])
    assert job["status"] == "failed" and "still 23178631" in job["message"]
    assert _status(client, wid)["auto_blocked_build"] == "23200000"
    assert client.portal.call(services.updates.tick) == []  # not retried
    assert services.worlds.get(w.id).auto_update_failed_build == "23200000"


def test_when_empty_waits_for_players_then_updates(client: TestClient, settings: Settings,
                                                   fake_docker: FakeDockerAdapter,
                                                   monkeypatch) -> None:
    """An older build shows "Update available"; "when empty" waits until
    0 players, then updates and returns online."""
    monkeypatch.setattr(world_service, "WAIT_EMPTY_POLL_S", 0.05)
    world = _create(client)
    wid = world["id"]
    services = _services(client)
    fake_docker.latest_build = "23200000"
    _status(client, wid, check=True)
    rt = services.monitor.runtime(wid)
    rt.players = 1
    jobs = client.portal.call(services.updates.tick)
    assert len(jobs) == 1
    wait_for(lambda: "Waiting for 1 player" in client.get(f"/api/jobs/{jobs[0]}").json()["message"])
    st = _status(client, wid)
    assert st["game"]["update_available"] and st["pending_job"]["id"] == jobs[0]
    assert client.portal.call(services.updates.tick) == []  # already queued
    time.sleep(0.2)
    assert _manifest_build(settings) == "23178631"  # still waiting
    rt.players = 0
    assert wait_job(client, jobs[0])["status"] == "succeeded"
    assert _manifest_build(settings) == "23200000"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    kinds = [e["kind"] for e in client.get(f"/api/worlds/{wid}/events").json()]
    assert "update_available" in kinds and "updated" in kinds


def test_manual_policy_and_window(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    services = _services(client)
    fake_docker.latest_build = "23200000"
    _status(client, wid, check=True)
    client.put(f"/api/worlds/{wid}/update-policy", json={"update_policy": "manual"})
    assert client.portal.call(services.updates.tick) == []

    res = client.put(f"/api/worlds/{wid}/update-policy",
                     json={"update_policy": "window", "update_window_cron": "nonsense"})
    assert res.status_code == 422
    res = client.put(f"/api/worlds/{wid}/update-policy",
                     json={"update_policy": "window", "update_window_cron": "0 4 * * *"})
    assert res.json()["next_window"]
    assert client.portal.call(services.updates.tick) == []  # not in the window
    services.updates._windows[wid].last = datetime.now(UTC) - timedelta(days=1)
    jobs = client.portal.call(services.updates.tick)
    assert len(jobs) == 1 and wait_job(client, jobs[0])["status"] == "succeeded"
    assert client.put(f"/api/worlds/{wid}/update-policy",
                      json={"update_policy": "sometimes"}).status_code == 422


# -- metrics and players ---------------------------------------------------------------


def test_metrics_and_host(client: TestClient) -> None:
    world = _create(client)
    wid = world["id"]
    assert wait_for(lambda: len(client.get(f"/api/worlds/{wid}/metrics?range=1h")
                                .json()["points"]) >= 1)
    m = client.get(f"/api/worlds/{wid}/metrics?range=24h").json()
    assert m["step_s"] == 300 and m["points"][0]["cpu_pct"] == 12.5
    assert m["points"][0]["mem_mb"] == 6144
    assert client.get(f"/api/worlds/{wid}/metrics?range=5y").status_code == 422
    summary = client.get("/api/worlds").json()[0]
    assert summary["metrics"]["cpu_pct"] == 12.5
    host = client.get("/api/system/host").json()
    assert host["worlds"] == 1 and host["mem_total"] and host["cpus"]


def test_player_history(client: TestClient) -> None:
    world = _create(client)
    wid = world["id"]
    now = datetime.now(UTC)
    with session_scope() as s:
        s.add(PlayerSession(world_id=wid, player_name="Wanderer",
                            joined_at=now - timedelta(hours=3), left_at=now - timedelta(hours=2)))
        s.add(PlayerSession(world_id=wid, player_name="Wanderer",
                            joined_at=now - timedelta(minutes=30), left_at=None))
        s.add(PlayerSession(world_id=wid, player_name="Ashwalker",
                            joined_at=now - timedelta(hours=5), left_at=now - timedelta(hours=4)))
    h = client.get(f"/api/worlds/{wid}/players/history").json()
    soul, grunt = h["players"]
    assert soul["player"] == "Wanderer" and soul["sessions"] == 2 and soul["online"]
    assert soul["playtime_s"] == pytest.approx(5400, abs=5)
    assert grunt["playtime_s"] == pytest.approx(3600, abs=5) and not grunt["online"]
    assert h["sessions"][0]["left_at"] is None
    events = client.get(f"/api/worlds/{wid}/events?kind=created&kind=started").json()
    assert {e["kind"] for e in events} <= {"created", "started"}
    first = client.get(f"/api/worlds/{wid}/events?limit=1").json()[0]
    older = client.get(f"/api/worlds/{wid}/events?before={first['id']}").json()
    assert all(e["id"] < first["id"] for e in older)
