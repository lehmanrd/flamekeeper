"""Scheduled restarts: schedule API, when they run, wait for players, and skips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Event, Schedule
from app.services.notifications import event_message
from tests.conftest import wait_for, wait_job
from tests.test_lifecycle import _create


def _setup(client: TestClient, **schedule) -> tuple[str, object]:
    """A running world with the restart schedule on, an old start and a due slot."""
    world = _create(client)
    wid = world["id"]
    wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    body = {"enabled": True, "cron": "0 5 * * *", **schedule}
    res = client.put(f"/api/worlds/{wid}/schedules/restart", json=body)
    assert res.status_code == 200, res.text
    _age_starts(wid, timedelta(days=1))
    _make_due(wid)
    return wid, client.app.state.services


def _make_due(wid: str) -> None:
    with session_scope() as s:
        s.get(Schedule, (wid, "restart")).last_run_at = datetime.now(UTC) - timedelta(days=1)


def _age_starts(wid: str, age: timedelta) -> None:
    with session_scope() as s:
        starts = Event.kind.in_(["online", "started"])
        for e in s.query(Event).filter(Event.world_id == wid, starts):
            e.ts = datetime.now(UTC) - age


def _run(client: TestClient, services, wid: str, at: datetime | None = None) -> str | None:
    world = services.worlds.get(wid)
    return client.portal.call(partial(services.restarts.run_due, world, at))


def _events(client: TestClient, wid: str, kind: str) -> list[dict]:
    return [e for e in client.get(f"/api/worlds/{wid}/events").json() if e["kind"] == kind]


def test_restart_schedule_api(client: TestClient) -> None:
    wid = _create(client)["id"]
    sched = client.get(f"/api/worlds/{wid}/schedules/restart").json()
    assert sched["enabled"] is False and sched["cron"] == "0 5 * * *"
    assert sched["only_when_empty"] and sched["max_wait_min"] == 120
    assert sched["if_still_busy"] == "skip" and sched["min_uptime_h"] == 2
    assert sched["next_run_at"] is None and len(sched["upcoming"]) == 3

    url = f"/api/worlds/{wid}/schedules/restart"
    for bad in ({"cron": "*/30 * * * *"}, {"cron": "bogus"}, {"max_wait_min": -1},
                {"max_wait_min": 5000}, {"min_uptime_h": 1000}, {"if_still_busy": "maybe"},
                {"retention_count": 3}):
        assert client.put(url, json=bad).status_code == 422, bad
    sched = client.put(url, json={"enabled": True, "cron": "0 */6 * * *",
                                  "if_still_busy": "restart"}).json()
    assert sched["enabled"] and sched["next_run_at"] and sched["if_still_busy"] == "restart"
    kinds = [s["kind"] for s in client.get(f"/api/worlds/{wid}/schedules").json()]
    assert kinds == ["backup", "restart"]


def test_due_restart_runs_when_empty(client: TestClient) -> None:
    wid, services = _setup(client)
    job = _run(client, services, wid)
    assert job and wait_job(client, job)["status"] == "succeeded"
    assert client.get(f"/api/jobs/{job}").json()["type"] == "restart"
    assert _events(client, wid, "scheduled_restart")[0]["data"]["outcome"] == "restarting"
    assert _events(client, wid, "started")[0]["data"]["reason"] == "scheduled"
    # The slot is used up: nothing more until the next one.
    assert _run(client, services, wid) is None


def test_not_due_disabled_or_stopped(client: TestClient) -> None:
    wid, services = _setup(client)
    with session_scope() as s:
        s.get(Schedule, (wid, "restart")).last_run_at = datetime.now(UTC)
    assert _run(client, services, wid) is None  # not due yet

    client.put(f"/api/worlds/{wid}/schedules/restart", json={"enabled": False})
    _make_due(wid)
    assert _run(client, services, wid) is None

    client.put(f"/api/worlds/{wid}/schedules/restart", json={"enabled": True})
    job = client.post(f"/api/worlds/{wid}/stop").json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded"
    _make_due(wid)
    assert _run(client, services, wid) is None
    assert _events(client, wid, "scheduled_restart") == []  # a stopped world isn't news


def test_waits_for_players_then_restarts(client: TestClient) -> None:
    wid, services = _setup(client)
    rt = services.monitor.runtime(wid)
    rt.players = 2
    start = datetime.now(UTC)
    assert _run(client, services, wid, start) is None
    sched = client.get(f"/api/worlds/{wid}/schedules/restart").json()
    assert sched["waiting_since"]
    assert _run(client, services, wid, start + timedelta(minutes=30)) is None  # still waiting
    rt.players = 0
    job = _run(client, services, wid, start + timedelta(minutes=31))
    assert job and wait_job(client, job)["status"] == "succeeded"
    assert client.get(f"/api/worlds/{wid}/schedules/restart").json()["waiting_since"] is None


def test_still_busy_after_the_wait_skips(client: TestClient) -> None:
    wid, services = _setup(client, max_wait_min=60)
    services.monitor.runtime(wid).players = 1
    start = datetime.now(UTC)
    assert _run(client, services, wid, start) is None
    assert _run(client, services, wid, start + timedelta(minutes=61)) is None
    skipped = _events(client, wid, "scheduled_restart")[0]["data"]
    assert skipped == {"outcome": "skipped", "reason": "players_online", "players": 1}
    services.monitor.runtime(wid).players = 0
    assert _run(client, services, wid, start + timedelta(minutes=62)) is None  # next slot


def test_still_busy_after_the_wait_restarts_anyway(client: TestClient) -> None:
    wid, services = _setup(client, max_wait_min=60, if_still_busy="restart")
    services.monitor.runtime(wid).players = 1
    start = datetime.now(UTC)
    assert _run(client, services, wid, start) is None
    job = _run(client, services, wid, start + timedelta(minutes=61))
    assert job and wait_job(client, job)["status"] == "succeeded"
    data = _events(client, wid, "scheduled_restart")[0]["data"]
    assert data == {"outcome": "restarting", "players": 1}


def test_not_waiting_for_empty_restarts_with_players(client: TestClient) -> None:
    wid, services = _setup(client, only_when_empty=False)
    services.monitor.runtime(wid).players = 3
    job = _run(client, services, wid)
    assert job and wait_job(client, job)["status"] == "succeeded"


def test_recent_start_skips(client: TestClient) -> None:
    wid, services = _setup(client, min_uptime_h=6)
    _age_starts(wid, timedelta(hours=5))
    assert _run(client, services, wid) is None
    assert _events(client, wid, "scheduled_restart")[0]["data"]["reason"] == "recent_start"

    # A server that only recovered from not answering is still up since its start.
    _make_due(wid)
    _age_starts(wid, timedelta(hours=7))
    services.events.record(wid, "online", recovered=True)
    job = _run(client, services, wid)
    assert job and wait_job(client, job)["status"] == "succeeded"


def test_busy_world_retries_and_restart_meanwhile_skips(client: TestClient,
                                                         monkeypatch) -> None:
    wid, services = _setup(client)
    monkeypatch.setattr(services.jobs, "is_busy", lambda world_id: True)
    start = datetime.now(UTC)
    assert _run(client, services, wid, start) is None
    assert client.get(f"/api/worlds/{wid}/schedules/restart").json()["waiting_since"]
    monkeypatch.undo()
    # The world came online again (an update restarted it) while this one waited.
    services.events.record(wid, "online")
    assert _run(client, services, wid, start + timedelta(minutes=5)) is None
    assert _events(client, wid, "scheduled_restart")[0]["data"]["reason"] == \
        "restarted_meanwhile"


def test_scheduler_pass_starts_due_restarts(client: TestClient) -> None:
    wid, services = _setup(client)
    jobs = client.portal.call(services.scheduler.run_restarts)
    assert len(jobs) == 1 and wait_job(client, jobs[0])["status"] == "succeeded"


def test_notification_messages() -> None:
    msg = event_message("scheduled_restart", "Embervale", {"outcome": "restarting",
                                                          "players": 0})
    assert msg["title"] == "🔄 Scheduled restart of Embervale" and "description" not in msg
    msg = event_message("scheduled_restart", "Embervale", {"outcome": "restarting",
                                                          "players": 2})
    assert "2 player(s)" in msg["description"]
    msg = event_message("scheduled_restart", "Embervale",
                        {"outcome": "skipped", "reason": "players_online", "players": 1})
    assert "skipped" in msg["title"] and "still online" in msg["description"]
