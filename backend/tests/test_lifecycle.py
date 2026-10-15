"""World lifecycle through the HTTP API against the fake Docker adapter."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.docker_adapter import ContainerSpec, RealDockerAdapter
from app.services.docker_fake import FakeDockerAdapter
from tests.conftest import wait_for, wait_job


def _create(client: TestClient, **body) -> dict:
    res = client.post("/api/worlds", json={"display_name": "Embervale", **body})
    assert res.status_code == 202, res.text
    data = res.json()
    job = wait_job(client, data["job_id"])
    assert job["status"] == "succeeded", client.get(f"/api/jobs/{data['job_id']}").json()
    return data["world"]


def test_health_and_selfcheck(client: TestClient) -> None:
    assert client.get("/api/health").json()["setup_error"] is None
    system = client.get("/api/system").json()
    assert {c["id"] for c in system["selfcheck"]["checks"]} >= {"docker", "data_dir", "disk"}
    schema = client.get("/api/settings-schema").json()
    assert schema["presets"][0] == "Default"


def test_create_world_writes_config_and_container(
    client: TestClient, fake_docker: FakeDockerAdapter, settings: Settings
) -> None:
    world = _create(client, slot_count=8)
    assert world["slug"] == "embervale"
    assert world["query_port"] == 15637

    cfg_path = settings.worlds_dir / "embervale" / "game" / "server" / "enshrouded_server.json"
    doc = json.loads(cfg_path.read_text())
    assert doc["name"] == "Embervale" and doc["slotCount"] == 8 and doc["queryPort"] == 15637
    assert [r["name"] for r in doc["userGroups"]] == ["Admin", "Friend", "Guest", "Visitor"]
    assert (settings.worlds_dir / "embervale" / "config-history" / "1.json").exists()

    spec: ContainerSpec = next(iter(fake_docker.specs.values()))
    assert spec.name == "flamekeeper-embervale"
    assert spec.labels["flamekeeper.managed"] == "true"
    assert spec.labels["flamekeeper.world_id"] == world["id"]
    assert spec.ports == {"15637/udp": ("0.0.0.0", 15637)}
    assert spec.binds == {str(settings.worlds_dir / "embervale" / "game"): "/opt/enshrouded"}
    assert spec.env["SERVER_QUERYPORT"] == "15637"
    # These would make the image overwrite our JSON on every container start.
    for key in ("SERVER_NAME", "SERVER_SLOT_COUNT", "SERVER_PASSWORD", "UPDATE_CRON",
                "BACKUP_CRON", "RESTART_CRON"):
        assert key not in spec.env
    assert not any(k.startswith(("SERVER_GS_", "SERVER_ROLE_")) for k in spec.env)

    online = wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"] == "online")
    assert online
    cfg = client.get(f"/api/worlds/{world['id']}/config").json()
    assert [r["has_password"] for r in cfg["roles"]] == [True] * 4
    assert not any("password" in r for r in cfg["roles"])


def test_create_validation(client: TestClient) -> None:
    _create(client)
    dup = client.post("/api/worlds", json={"display_name": "Embervale"})
    assert dup.status_code == 409
    assert dup.headers["content-type"].startswith("application/problem+json")
    port_clash = client.post("/api/worlds", json={"display_name": "Other", "query_port": 15637})
    assert port_clash.status_code == 409
    bad_slug = client.post("/api/worlds", json={"display_name": "X", "slug": "-bad-"})
    assert bad_slug.status_code == 400
    too_many = client.post("/api/worlds", json={"display_name": "X", "slot_count": 17})
    assert too_many.status_code == 422


def test_port_suggestion_skips_worlds_and_host_ports(
    client: TestClient, fake_docker: FakeDockerAdapter
) -> None:
    _create(client)
    fake_docker.host_udp_ports = {15638}
    assert client.get("/api/worlds/suggest-port").json()["port"] == 15639
    taken = client.post("/api/worlds", json={"display_name": "B", "query_port": 15638})
    assert taken.status_code == 409


def test_stop_start_restart(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]

    job = wait_job(client, client.post(f"/api/worlds/{wid}/stop").json()["job_id"])
    assert job["status"] == "succeeded"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "stopped")
    assert client.get(f"/api/worlds/{wid}").json()["desired_state"] == "stopped"

    job = wait_job(client, client.post(f"/api/worlds/{wid}/start").json()["job_id"])
    assert job["status"] == "succeeded"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")

    res = client.post(f"/api/worlds/{wid}/restart", json={"mode": "process"})
    job = wait_job(client, res.json()["job_id"])
    assert job["status"] == "succeeded"
    assert ("flamekeeper-embervale", ["supervisorctl", "stop", "enshrouded-server"]) in \
        fake_docker.exec_calls

    res = client.post(f"/api/worlds/{wid}/restart", json={"mode": "container"})
    assert wait_job(client, res.json()["job_id"])["status"] == "succeeded"
    assert fake_docker.calls[-2:] == [("stop", "flamekeeper-embervale"),
                                      ("start", "flamekeeper-embervale")]


def test_process_restart_waits_for_new_host_online(
    client: TestClient, fake_docker: FakeDockerAdapter
) -> None:
    """An old HostOnline line in the same container must not satisfy the wait."""
    world = _create(client)
    fake_docker.emit_host_online = False
    res = client.post(f"/api/worlds/{world['id']}/restart", json={"mode": "process"})
    job_id = res.json()["job_id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "running")
    import time

    time.sleep(0.3)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "running"
    fake_docker.emit_log("flamekeeper-embervale",
                         "x supervisord: enshrouded-server [Session] 'HostOnline' (up)!")
    assert wait_job(client, job_id)["status"] == "succeeded"


def test_bad_config_fails_start_fast_with_hint(
    client: TestClient, fake_docker: FakeDockerAdapter
) -> None:
    world = _create(client)
    wait_job(client, client.post(f"/api/worlds/{world['id']}/stop").json()["job_id"])
    fake_docker.emit_host_online = False
    job_id = client.post(f"/api/worlds/{world['id']}/start").json()["job_id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "running")
    fake_docker.emit_log(
        "flamekeeper-embervale",
        "x supervisord: enshrouded-server Failed to parse enum 'CurseModifier: Invalid value "
        "'Nope'.",
    )
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "Invalid value 'Nope'" in job["message"]
    assert "HINT:" in client.get(f"/api/jobs/{job_id}").json()["log"]


def test_crash_is_detected(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    fake_docker.crash("flamekeeper-embervale", exit_code=139)
    assert wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"]
                    == "crashed")
    kinds = [e["kind"] for e in client.get(f"/api/worlds/{world['id']}/events").json()]
    assert "crashed" in kinds


def test_start_recreates_a_missing_network(client: TestClient,
                                         fake_docker: FakeDockerAdapter) -> None:
    """A world created stopped, whose network was removed before its first start."""
    world = _create(client, start=False)
    fake_docker.networks.discard("flamekeeper")
    job = wait_job(client, client.post(f"/api/worlds/{world['id']}/start").json()["job_id"])
    assert job["status"] == "succeeded", job
    assert "flamekeeper" in fake_docker.networks


def test_network_lookup_ignores_partial_name_matches() -> None:
    """Docker's name filter matches partial names, so the compose project's
    "flamekeeper_default" network must not count as "flamekeeper"."""
    created: list[str] = []

    class Net:
        def __init__(self, name: str) -> None:
            self.name = name

    class Networks:
        def list(self, names: list[str]) -> list[Net]:
            return [Net("flamekeeper_default")]

        def create(self, name: str, **kwargs) -> None:
            created.append(name)

    adapter = object.__new__(RealDockerAdapter)
    adapter.client = SimpleNamespace(networks=Networks())
    adapter._docker = SimpleNamespace(errors=SimpleNamespace(APIError=RuntimeError))
    asyncio.run(adapter.ensure_network("flamekeeper"))
    assert created == ["flamekeeper"]


def test_missing_container_and_recreate(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    fake_docker.containers.clear()
    assert wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"]
                    == "missing")
    job = wait_job(client, client.post(f"/api/worlds/{world['id']}/start").json()["job_id"])
    assert job["status"] == "succeeded"
    assert wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"] == "online")


def test_delete_keeps_or_removes_data(client: TestClient, settings: Settings) -> None:
    a = _create(client)
    b = _create(client, display_name="Second")
    job = wait_job(client, client.delete(f"/api/worlds/{a['id']}").json()["job_id"])
    assert job["status"] == "succeeded"
    assert (settings.worlds_dir / "embervale").exists()
    assert client.get(f"/api/worlds/{a['id']}").status_code == 404

    job = wait_job(client,
                   client.delete(f"/api/worlds/{b['id']}?delete_data=true").json()["job_id"])
    assert job["status"] == "succeeded"
    assert not (settings.worlds_dir / "second").exists()
    assert client.get("/api/worlds").json() == []


def test_logs_endpoint(client: TestClient) -> None:
    world = _create(client)
    lines = client.get(f"/api/worlds/{world['id']}/logs").json()["lines"]
    assert any("HostOnline" in ln["text"] for ln in lines)
    text = client.get(f"/api/worlds/{world['id']}/logs/download").text
    assert "HostOnline" in text


def test_jobs_listing_and_cancel(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    jobs = client.get(f"/api/jobs?world_id={world['id']}").json()
    assert jobs[0]["type"] == "create" and jobs[0]["status"] == "succeeded"
    fake_docker.emit_host_online = False
    job_id = client.post(f"/api/worlds/{world['id']}/restart").json()["job_id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "running")
    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}
    assert wait_job(client, job_id)["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_unknown_world_is_404(client: TestClient) -> None:
    res = client.get("/api/worlds/nope")
    assert res.status_code == 404
    assert res.json()["title"] == "Not Found"


def test_world_lookup_by_slug(client: TestClient) -> None:
    world = _create(client)
    assert client.get("/api/worlds/embervale").json()["id"] == world["id"]


def test_spa_fallback_and_base_path(tmp_path: Path, fake_docker: FakeDockerAdapter) -> None:
    from app.main import create_app

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html><head></head><body>app</body></html>")
    (static / "assets" / "a.js").write_text("js")
    settings = Settings(data_dir=tmp_path / "d", fake_docker=True, skip_path_check=True,
                        static_dir=static, base_path="/fk")
    with TestClient(create_app(settings, docker=fake_docker, poll_interval=0.05)) as c:
        page = c.get("/fk/worlds/embervale")
        assert page.status_code == 200
        assert '<base href="/fk/">' in page.text and 'window.__FK_BASE__ = "/fk"' in page.text
        assert c.get("/fk/assets/a.js").text == "js"
        assert c.get("/fk/api/health").json()["status"] == "ok"
        assert c.get("/fk/api/nope").status_code == 404
        assert c.get("/", follow_redirects=False).headers["location"] == "/fk/"


def test_restarts_by_flamekeeper_are_not_a_crash_loop(client: TestClient) -> None:
    """Several starts in a few minutes, all asked for, then a container restart: the
    server is online, not crash-looping."""
    world = _create(client)
    wid = world["id"]
    for _ in range(2):
        assert wait_job(client, client.post(f"/api/worlds/{wid}/stop", json={}).json()
                        ["job_id"])["status"] == "succeeded"
        assert wait_job(client, client.post(f"/api/worlds/{wid}/start").json()
                        ["job_id"])["status"] == "succeeded"
    for mode in ("process", "container"):
        job = client.post(f"/api/worlds/{wid}/restart", json={"mode": mode}).json()
        assert wait_job(client, job["job_id"])["status"] == "succeeded"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    time.sleep(0.5)
    assert client.get(f"/api/worlds/{wid}").json()["state"] == "online"
