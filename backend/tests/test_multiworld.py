"""The create wizard's API, capacity warnings, app settings, and three worlds
running side by side."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.capacity import capacity
from app.services.docker_fake import FakeDockerAdapter
from tests.conftest import wait_for, wait_job

PERMS = {"canKickBan": False, "canAccessInventories": True, "canEditWorld": True,
         "canEditBase": True, "canExtendBase": False, "reservedSlots": 0}


def _server_dir(settings: Settings, slug: str) -> Path:
    return settings.worlds_dir / slug / "game" / "server"


def _doc(settings: Settings, slug: str) -> dict:
    return json.loads((_server_dir(settings, slug) / "enshrouded_server.json").read_text())


def _wizard(client: TestClient, **body) -> dict:
    res = client.post("/api/worlds", json=body)
    assert res.status_code == 202, res.text
    job = wait_job(client, res.json()["job_id"])
    assert job["status"] == "succeeded", client.get(f"/api/jobs/{job['id']}").json()
    return res.json()["world"]


def test_capacity_warnings(tmp_path: Path) -> None:
    gb = 1024**3
    ok = capacity(tmp_path, worlds=0, extra=1, mem_total=32 * gb, cores=12)
    assert not [w for w in ok["warnings"] if "RAM" in w or "cores" in w]
    tight = capacity(tmp_path, worlds=2, extra=1, mem_total=16 * gb, cores=8)
    assert any("48 GB of RAM" in w for w in tight["warnings"])
    assert any("18 CPU cores" in w for w in tight["warnings"])
    assert tight["recommended"]["ram"] == 48 * gb


def test_wizard_create_with_roles_policy_and_backups(client: TestClient,
                                                    settings: Settings) -> None:
    roles = [
        {"name": "Admin", "password": "ember-otter-lantern-moss", **PERMS, "canKickBan": True},
        {"name": "Friend", "password": "pine-raven-cliff-sage", **PERMS},
    ]
    body = {"display_name": "Frostmere", "slot_count": 6, "preset": "Survival",
            "roles": roles, "game_branch": "public", "update_policy": "manual",
            "backup": {"enabled": True, "cron": "*/30 * * * *", "retention_count": 10},
            "start": False}
    preview = client.post("/api/worlds?dry_run=true", json=body).json()
    assert preview["slug"] == "frostmere" and preview["query_port"] == 15637
    assert preview["document"]["gameSettingsPreset"] == "Survival"
    assert {r["password"] for r in preview["document"]["userGroups"]} == {"********"}
    assert "warnings" in preview["capacity"]
    assert client.get("/api/worlds").json() == []  # dry run created nothing

    world = _wizard(client, **body)
    assert world["desired_state"] == "stopped"
    doc = _doc(settings, "frostmere")
    assert [r["name"] for r in doc["userGroups"]] == ["Admin", "Friend"]
    assert doc["userGroups"][1]["password"] == "pine-raven-cliff-sage"
    assert (doc["gameSettingsPreset"], doc["slotCount"]) == ("Survival", 6)
    status = client.get(f"/api/worlds/{world['id']}/update").json()
    assert status["policy"]["update_policy"] == "manual"
    sched = client.get(f"/api/worlds/{world['id']}/backups").json()["schedule"]
    assert (sched["cron"], sched["retention_count"]) == ("*/30 * * * *", 10)
    assert wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"] == "stopped")


def test_wizard_validation_leaves_nothing_behind(client: TestClient) -> None:
    bad_roles = [{"name": "A", "password": "same", **PERMS},
                 {"name": "B", "password": "same", **PERMS}]
    res = client.post("/api/worlds", json={"display_name": "X", "roles": bad_roles})
    assert res.status_code == 422 and "Roles:" in res.text
    res = client.post("/api/worlds", json={"display_name": "X", "game_branch": "bad branch!"})
    assert res.status_code == 422
    res = client.post("/api/worlds", json={"display_name": "X",
                                           "backup": {"cron": "*/2 * * * *"}})
    assert res.status_code == 422
    res = client.post("/api/worlds", json={"display_name": "X", "copy_game_from": "nope"})
    assert res.status_code == 404
    assert client.get("/api/worlds").json() == []


def test_check_port(client: TestClient, fake_docker: FakeDockerAdapter) -> None:
    world = _wizard(client, display_name="Embervale")
    fake_docker.host_udp_ports.add(15700)
    check = lambda port, **q: client.get("/api/worlds/check-port",  # noqa: E731
                                         params={"port": port, **q}).json()
    assert check(15638) == {"port": 15638, "free": True, "reason": None}
    assert check(15637)["reason"] == "Used by Embervale"
    assert check(15637, world_id=world["id"])["free"]
    assert check(15700)["reason"] == "In use by another program"
    assert not check(80)["free"]


def test_copy_game_files_from_another_world(client: TestClient, settings: Settings) -> None:
    src = _wizard(client, display_name="Embervale")
    server = _server_dir(settings, "embervale")
    (server / "enshrouded_server.exe").write_bytes(b"x" * 4096)
    pfx = server / "steamapps" / "compatdata" / "2278520" / "pfx"
    (pfx / "drive_c" / "windows").mkdir(parents=True)
    (pfx / "dosdevices").mkdir()
    (pfx / "dosdevices" / "c:").symlink_to("../drive_c")  # Wine needs these links
    (server / "logs").mkdir(exist_ok=True)
    (server / "logs" / "enshrouded_server.log").write_text("old log")
    world = _wizard(client, display_name="Copycat", copy_game_from=src["id"])
    new = _server_dir(settings, "copycat")
    assert (new / "enshrouded_server.exe").read_bytes() == b"x" * 4096
    assert (new / "steamapps" / "appmanifest_2278520.acf").exists()
    assert not (new / "logs" / "enshrouded_server.log").exists()
    link = new / "steamapps" / "compatdata" / "2278520" / "pfx" / "dosdevices" / "c:"
    assert link.is_symlink() and str(link.readlink()) == "../drive_c"
    assert (link / "windows").is_dir()
    assert _doc(settings, "copycat")["name"] == "Copycat"
    saves = list((new / "savegame").iterdir())
    assert saves and (new / "savegame" / "3ad85aea").read_bytes() != \
        (server / "savegame" / "3ad85aea").read_bytes()  # its own new world
    log = client.get(f"/api/jobs?world_id={world['id']}").json()[0]
    assert "Copied" in client.get(f"/api/jobs/{log['id']}").json()["log"]


def test_app_settings(client: TestClient) -> None:
    view = client.get("/api/settings/app").json()
    assert view["values"]["default_image_tag"] == "latest" and view["overridden"] == []
    res = client.put("/api/settings/app", json={"default_image_tag": "stable-wine",
                                                "first_query_port": 16000})
    assert res.status_code == 200 and sorted(res.json()["overridden"]) == \
        ["default_image_tag", "first_query_port"]
    world = _wizard(client, display_name="Winey")
    assert world["image"].endswith(":stable-wine") and world["query_port"] == 16000
    assert client.put("/api/settings/app", json={"first_query_port": 80}).status_code == 422
    assert client.put("/api/settings/app", json={"data_dir": "/"}).status_code == 422
    res = client.put("/api/settings/app", json={"default_image_tag": "latest"})
    assert res.json()["overridden"] == ["first_query_port"]
    assert client.get("/api/system/capacity?extra=1").json()["worlds"] == 2


def test_three_worlds_side_by_side(client: TestClient, settings: Settings,
                                   fake_docker: FakeDockerAdapter) -> None:
    """Three worlds on 15637/15638/15639 with different presets and roles;
    each can be stopped, updated and restored independently."""
    plans = [("Embervale", "Default", "Admin"), ("Frostmere", "Survival", "Warden"),
             ("Kids' Valley", "Relaxed", "Parent")]
    worlds = []
    for name, preset, role in plans:
        roles = [{"name": role, "password": f"{role.lower()}-secret", **PERMS}]
        worlds.append(_wizard(client, display_name=name, preset=preset, roles=roles))
    assert [w["query_port"] for w in worlds] == [15637, 15638, 15639]
    assert [w["slug"] for w in worlds] == ["embervale", "frostmere", "kids-valley"]
    online = lambda w: client.get(f"/api/worlds/{w['id']}").json()["state"] == "online"  # noqa: E731
    assert wait_for(lambda: all(online(w) for w in worlds))
    for w, (_, preset, role) in zip(worlds, plans, strict=True):
        doc = _doc(settings, w["slug"])
        assert doc["gameSettingsPreset"] == preset and doc["userGroups"][0]["name"] == role
        spec = fake_docker.spec_of(f"flamekeeper-{w['slug']}")
        assert spec.ports == {f"{w['query_port']}/udp": ("0.0.0.0", w["query_port"])}

    emb, frost, kids = worlds
    # Stop one: the others stay online.
    wait_job(client, client.post(f"/api/worlds/{frost['id']}/stop").json()["job_id"])
    assert wait_for(lambda: client.get(f"/api/worlds/{frost['id']}").json()["state"]
                    == "stopped")
    assert online(emb) and online(kids)

    # Update one: only it changes build. (Stop the scheduler first: with the default
    # "when empty" policy it would rightly auto-update the other running world too.)
    client.portal.call(client.app.state.services.scheduler.stop)
    fake_docker.latest_build = "23200000"
    client.post(f"/api/worlds/{emb['id']}/update/check")
    job = client.post(f"/api/worlds/{emb['id']}/update", json={}).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded"
    builds = {w["slug"]: client.get(f"/api/worlds/{w['id']}/update").json()["game"]["installed"]
              for w in worlds}
    assert builds == {"embervale": "23200000", "frostmere": "23178631",
                      "kids-valley": "23178631"}

    # Restore one: the others' saves are untouched.
    kids_save = _server_dir(settings, "kids-valley") / "savegame" / "3ad85aea"
    kids_save.write_bytes(b"kids castle")
    job = client.post(f"/api/worlds/{kids['id']}/backups", json={"mode": "last_save"}).json()
    assert wait_job(client, job["job_id"])["status"] == "succeeded"
    backup = client.get(f"/api/worlds/{kids['id']}/backups").json()["backups"][0]
    kids_save.write_bytes(b"destroyed")
    emb_before = (_server_dir(settings, "embervale") / "savegame" / "3ad85aea").read_bytes()
    job = client.post(f"/api/worlds/{kids['id']}/backups/{backup['id']}/restore", json={})
    assert wait_job(client, job.json()["job_id"])["status"] == "succeeded"
    assert kids_save.read_bytes() == b"kids castle"
    assert (_server_dir(settings, "embervale") / "savegame" / "3ad85aea").read_bytes() == \
        emb_before
    assert wait_for(lambda: online(kids) and online(emb))
    assert client.get(f"/api/worlds/{frost['id']}").json()["state"] == "stopped"
