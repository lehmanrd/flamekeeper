"""Service-level tests: reconcile, job serialization, controller restart."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.bus import Bus
from app.services.docker_adapter import ContainerSpec
from app.services.docker_fake import FakeDockerAdapter
from app.services.jobs import JobManager
from tests.conftest import wait_for, wait_job


def _spec(name: str, labels: dict[str, str], port: int = 15650) -> ContainerSpec:
    return ContainerSpec(name=name, image="mornedhels/enshrouded-server:stable-wine",
                         hostname=name, labels=labels, env={"SERVER_QUERYPORT": str(port),
                                                            "PUID": "1000", "PGID": "1000"},
                         ports={}, binds={})


def test_reconcile_adopts_labeled_and_ignores_unlabeled(
    settings: Settings, fake_docker: FakeDockerAdapter
) -> None:
    async def seed() -> None:
        await fake_docker.create_container(_spec(
            "flamekeeper-orphan",
            {"flamekeeper.managed": "true", "flamekeeper.world_id": "w-orphan"},
        ))
        await fake_docker.start("flamekeeper-orphan")
        await fake_docker.create_container(_spec("someone-elses", {}, 15651))

    asyncio.run(seed())
    app = create_app(settings, docker=fake_docker, poll_interval=0.05)
    with TestClient(app) as client:
        worlds = client.get("/api/worlds").json()
        assert [w["slug"] for w in worlds] == ["orphan"]
        w = client.get("/api/worlds/orphan").json()
        assert w["id"] == "w-orphan" and w["query_port"] == 15650
        assert w["infra"]["image_tag"] == "stable-wine"
        kinds = [e["kind"] for e in client.get("/api/worlds/orphan/events").json()]
        assert "adopted" in kinds
    assert fake_docker._find("someone-elses") is not None


def test_controller_restart_leaves_game_running_and_readopts(
    settings: Settings, fake_docker: FakeDockerAdapter
) -> None:
    app = create_app(settings, docker=fake_docker, poll_interval=0.05)
    with TestClient(app) as client:
        res = client.post("/api/worlds", json={"display_name": "Keep"})
        assert wait_job(client, res.json()["job_id"])["status"] == "succeeded"
        wid = res.json()["world"]["id"]
    # Controller stopped: the game container is untouched.
    assert fake_docker._find("flamekeeper-keep").status == "running"
    assert ("stop", "flamekeeper-keep") not in fake_docker.calls

    app2 = create_app(settings, docker=fake_docker, poll_interval=0.05)
    with TestClient(app2) as client:
        assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")


def test_jobs_on_one_world_are_serialized(db: Settings) -> None:
    from app.db import session_scope
    from app.models import World

    with session_scope() as s:
        s.add(World(id="w1", slug="w1", display_name="W1", query_port=15700))

    async def scenario() -> list[str]:
        jobs = JobManager(Bus())
        order: list[str] = []

        def make(tag: str):
            async def fn(ctx) -> None:
                order.append(f"{tag}-start")
                await asyncio.sleep(0.05)
                order.append(f"{tag}-end")
            return fn

        a = jobs.submit("t", None, make("other"))  # no world: not serialized
        b = jobs.submit("t", "w1", make("b"))
        c = jobs.submit("t", "w1", make("c"))
        for j in (a, b, c):
            await jobs.wait(j)
        return order

    order = asyncio.run(scenario())
    assert order.index("b-end") < order.index("c-start")


def test_job_log_redacts_passwords(db: Settings) -> None:
    from app.db import session_scope
    from app.models import Job

    async def scenario() -> str:
        jobs = JobManager(Bus())

        async def fn(ctx) -> None:
            ctx.log('wrote {"password": "Secret123"}')

        job_id = jobs.submit("t", None, fn)
        await jobs.wait(job_id)
        return job_id

    job_id = asyncio.run(scenario())
    with session_scope() as s:
        log = s.get(Job, job_id).log
    assert "Secret123" not in log and '"password": "***"' in log


def test_interrupted_jobs_marked_failed(db: Settings) -> None:
    from app.db import session_scope
    from app.models import Job

    with session_scope() as s:
        s.add(Job(id="j1", type="create", status="running"))
    assert JobManager(Bus()).recover_interrupted() == 1
    with session_scope() as s:
        job = s.get(Job, "j1")
        assert job.status == "failed" and "Interrupted" in job.message


def test_sessions_closed_when_server_reports_no_players(client: TestClient) -> None:
    """A kicked/banned player gets no "Remove Player" log line (only "Disconnecting
    peer"), so their session is closed once A2S reports 0 players twice."""
    from datetime import UTC, datetime, timedelta

    from app.db import session_scope
    from app.models import PlayerSession
    from tests.test_lifecycle import _create

    world = _create(client)
    monitor = client.app.state.services.monitor
    w = client.app.state.services.worlds.get(world["id"])
    with session_scope() as s:
        s.add(PlayerSession(world_id=w.id, player_name="Ashwalker",
                            joined_at=datetime.now(UTC) - timedelta(minutes=5)))
        s.add(PlayerSession(world_id=w.id, player_name="JustJoined",
                            joined_at=datetime.now(UTC)))
    rt = monitor.runtime(w.id)
    rt.players = 0
    rt.zero_polls = 0
    client.portal.call(monitor._reconcile_sessions, w, rt)
    assert len(monitor.online_players(w.id)) == 2  # one poll isn't enough
    client.portal.call(monitor._reconcile_sessions, w, rt)
    assert [p["name"] for p in monitor.online_players(w.id)] == ["JustJoined"]
    left = [e for e in client.get(f"/api/worlds/{w.id}/events").json()
            if e["kind"] == "player_left"]
    assert left[0]["data"] == {"player": "Ashwalker", "reason": "disconnected"}
