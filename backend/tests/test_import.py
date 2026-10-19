"""Importing servers FlameKeeper didn't create: containers, host folders and zips."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.docker_adapter import Mount
from app.services.docker_fake import HOST_ONLINE_LINE, FakeDockerAdapter
from app.services.import_service import sanitize
from tests.conftest import wait_for, wait_job

ADMIN = {"name": "Admin", "password": "AdminPw1", "canKickBan": True,
         "canAccessInventories": True, "canEditBase": True, "canExtendBase": True,
         "reservedSlots": 0}
BAN = {"accountId": 12345678901234567891, "displayName": "Ashwalker",
       "characterName": "Emberling", "banDate": {"value": 1792031497}}
CONFIG = {
    "name": "Old Embervale",
    "saveDirectory": "./savegame",
    "logDirectory": "./logs",
    "ip": "0.0.0.0",
    "queryPort": 15637,
    "slotCount": 8,
    "gameSettingsPreset": "Custom",
    "gameSettings": {"enemyDamageFactor": 2, "bossDamageFactor": 99, "bogusSetting": 1},
    "userGroups": [ADMIN],
    "bannedAccounts": [BAN],
}


def _mornedhels(tmp: Path, fake: FakeDockerAdapter, *, running: bool = True,
                config: dict | None = None, **kwargs) -> Path:
    """The mornedhels layout: everything under one bind mount at /opt/enshrouded."""
    root = tmp / "old-server"
    save = root / "server" / "savegame"
    (save / "backup").mkdir(parents=True)
    (save / "world0.ksd").write_bytes(b"the old world")
    (save / "backup" / "world0.ksd.backup-1").write_bytes(b"an older copy")
    (root / "server" / "enshrouded_server.json").write_text(json.dumps(config or CONFIG))
    fake.add_container("enshrouded", "mornedhels/enshrouded-server:latest",
                       mounts=[Mount("bind", "/opt/enshrouded", str(root))],
                       ports={"15637/udp": [15637]}, running=running, **kwargs)
    if running:
        fake.host_udp_ports.add(15637)
    return save


def _new_save(settings: Settings, slug: str) -> Path:
    return settings.worlds_dir / slug / "game" / "server" / "savegame"


def _live_config(settings: Settings, slug: str) -> dict:
    return json.loads((settings.worlds_dir / slug / "game" / "server" /
                       "enshrouded_server.json").read_text())


def _scan(client: TestClient, **body) -> dict:
    res = client.post("/api/imports/scan", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _import(client: TestClient, token: str, **body) -> dict:
    res = client.post(f"/api/imports/{token}/create",
                      json={"display_name": "Embervale", **body})
    assert res.status_code == 202, res.text
    data = res.json()
    job = wait_job(client, data["job_id"])
    assert job["status"] == "succeeded", client.get(f"/api/jobs/{data['job_id']}").json()
    return data["world"]


def test_candidates_put_enshrouded_containers_first(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    fake_docker.add_container("web", "nginx:latest")
    _mornedhels(tmp_path, fake_docker)
    found = client.get("/api/imports/candidates").json()
    assert [c["name"] for c in found] == ["enshrouded", "web"]
    assert found[0]["likely"] and not found[1]["likely"]
    assert found[0]["mounts"][0]["destination"] == "/opt/enshrouded"


def test_import_container_end_to_end(
    client: TestClient, fake_docker: FakeDockerAdapter, settings: Settings, tmp_path: Path
) -> None:
    old_save = _mornedhels(tmp_path, fake_docker)
    scan = _scan(client, kind="container", container="enshrouded")
    assert scan["config_path"] == "/opt/enshrouded/server/enshrouded_server.json"
    assert scan["save_path"] == "/opt/enshrouded/server/savegame"
    s = scan["summary"]
    assert s["name"] == "Old Embervale" and s["slot_count"] == 8 and s["bans"] == 1
    assert s["save_format"] == "1.0" and s["save_files"] == 2
    assert s["roles"] == [{"name": "Admin", "has_password": True, "reserved_slots": 0}]
    assert "AdminPw1" not in json.dumps(scan)
    assert scan["port"] == 15637 and scan["reusable_port"] == 15637
    assert any("bogusSetting" in w for w in scan["warnings"])
    assert any("Boss" in w for w in scan["warnings"])
    assert scan["running"] and scan["players_online"] is None
    # Scanning changes nothing.
    assert fake_docker._find("enshrouded").status == "running"

    # Nobody can tell whether players are online: the admin has to confirm.
    res = client.post(f"/api/imports/{scan['token']}/create",
                      json={"display_name": "Embervale"})
    assert res.status_code == 409 and "can't tell" in res.json()["detail"]
    preview = client.post(f"/api/imports/{scan['token']}/create?dry_run=true",
                          json={"display_name": "Embervale", "query_port": 15637,
                                "confirm_players": True})
    assert preview.status_code == 202, preview.text
    assert fake_docker._find("enshrouded").status == "running"

    world = _import(client, scan["token"], query_port=15637, confirm_players=True)
    old = fake_docker._find("enshrouded")
    assert old.status == "exited" and old.restart_policy == "no"
    assert ("update", "enshrouded:no") in fake_docker.calls
    assert world["infra"]["query_port"] == 15637

    new_save = _new_save(settings, world["slug"])
    assert (new_save / "world0.ksd").read_bytes() == (old_save / "world0.ksd").read_bytes()
    assert (new_save / "backup" / "world0.ksd.backup-1").read_bytes() == b"an older copy"
    doc = _live_config(settings, world["slug"])
    assert doc["name"] == "Embervale" and doc["queryPort"] == 15637
    assert doc["gameSettingsPreset"] == "Custom"
    assert doc["gameSettings"]["enemyDamageFactor"] == 2
    assert doc["gameSettings"]["bossDamageFactor"] == 1  # invalid: the default
    assert "bogusSetting" not in doc["gameSettings"]
    assert doc["userGroups"][0]["name"] == "Admin"
    assert doc["userGroups"][0]["password"] == "AdminPw1"
    assert [r["name"] for r in doc["userGroups"]] == ["Admin"]
    assert doc["bannedAccounts"][0]["accountId"] == BAN["accountId"]

    backups = client.get(f"/api/worlds/{world['id']}/backups").json()["backups"]
    imported = [b for b in backups if b["kind"] == "imported"]
    assert len(imported) == 1 and imported[0]["note"] == "Imported from enshrouded"
    assert not (settings.imports_dir / scan["token"]).exists()

    info = client.get(f"/api/worlds/{world['id']}/import").json()["import"]
    assert info["state"] == "imported" and info["container"]["name"] == "enshrouded"
    candidates = client.get("/api/imports/candidates").json()
    assert candidates[0]["imported_into"] == world["slug"]


def test_players_online_block_until_confirmed(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    _mornedhels(tmp_path, fake_docker)
    fake_docker.emit_log("enshrouded", HOST_ONLINE_LINE)
    fake_docker.emit_log("enshrouded", "[server] Player 'Ash' logged in with Permissions:")
    fake_docker.emit_log("enshrouded", "[server] Player 'Bo' logged in with Permissions:")
    fake_docker.emit_log("enshrouded", "[server] Remove Player 'Bo'")
    scan = _scan(client, kind="container", container="enshrouded")
    assert scan["players_online"] == 1
    res = client.post(f"/api/imports/{scan['token']}/create",
                      json={"display_name": "Embervale", "query_port": 15637})
    assert res.status_code == 409 and "1 player(s)" in res.json()["detail"]

    fake_docker.emit_log("enshrouded", "[server] Remove Player 'Ash'")
    _import(client, scan["token"], query_port=15637)


def test_failed_import_puts_the_old_server_back(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    old_save = _mornedhels(tmp_path, fake_docker, restart_policy="always")
    scan = _scan(client, kind="container", container="enshrouded")
    shutil.rmtree(old_save)  # the copy after stopping fails
    res = client.post(f"/api/imports/{scan['token']}/create",
                      json={"display_name": "Embervale", "query_port": 15637,
                            "confirm_players": True})
    assert res.status_code == 202, res.text
    data = res.json()
    assert wait_job(client, data["job_id"])["status"] == "failed"

    old = fake_docker._find("enshrouded")
    wait_for(lambda: old.status == "running" and old.restart_policy == "always")
    info = wait_for(lambda: client.get(f"/api/worlds/{data['world']['id']}/import")
                    .json()["import"]["state"] == "rolled_back")
    assert info
    res = client.post(f"/api/worlds/{data['world']['id']}/import/remove-container")
    assert res.status_code == 409


def test_cleanup_container_then_volumes(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    save = tmp_path / "vol" / "savegame"
    save.mkdir(parents=True)
    (save / "world0.ksd").write_bytes(b"w")
    fake_docker.add_container("ens", "custom/enshrouded:1", mounts=[
        Mount("volume", "/data", str(tmp_path / "vol"), name="ens-data"),
        Mount("volume", "/shared", str(tmp_path / "shared"), name="shared-data"),
    ], running=False)
    (tmp_path / "shared").mkdir()
    fake_docker.add_container("other", "busybox", mounts=[
        Mount("volume", "/shared", str(tmp_path / "shared"), name="shared-data")])
    scan = _scan(client, kind="container", container="ens")
    assert scan["volumes"] == ["ens-data", "shared-data"]
    assert scan["players_online"] == 0
    world = _import(client, scan["token"])
    wid = world["id"]

    assert client.post(f"/api/worlds/{wid}/import/remove-volumes").status_code == 409
    res = client.post(f"/api/worlds/{wid}/import/remove-container")
    assert res.status_code == 200, res.text
    assert fake_docker._find("ens") is None
    res = client.post(f"/api/worlds/{wid}/import/remove-volumes").json()
    assert res["removed"] == ["ens-data"]
    assert res["kept"] == [{"name": "shared-data", "used_by": ["other"]}]
    assert res["import"]["state"] == "container_removed"
    assert client.post(f"/api/worlds/{wid}/import/dismiss").status_code == 204
    info = client.get(f"/api/worlds/{wid}/import").json()["import"]
    assert info["state"] == "dismissed"


def test_volume_save_with_config_in_the_container_layer(
    client: TestClient, fake_docker: FakeDockerAdapter, settings: Settings, tmp_path: Path
) -> None:
    """The sknnr layout: only the savegame is a volume; the config is generated from
    environment variables into the container's own files. Pre-1.0 save, legacy password."""
    vol = tmp_path / "volume"
    vol.mkdir()
    (vol / "3ad85aea").write_bytes(b"pre-1.0 world")
    (vol / "3ad85aea-index").write_text('{"latest": 0}')
    install = tmp_path / "layer"
    install.mkdir()
    (install / "enshrouded_server.json").write_text(json.dumps(
        {"name": "Env Server", "password": "letmein", "saveDirectory": "./savegame",
         "logDirectory": "./logs", "ip": "0.0.0.0", "queryPort": 15637, "slotCount": 16}))
    fake_docker.add_container(
        "sknnr", "sknnr/enshrouded-dedicated-server:latest",
        mounts=[Mount("volume", "/home/steam/enshrouded/savegame", str(vol), name="ens-save")],
        layer={"/home/steam/enshrouded": install},
        ports={"15637/udp": [24000]}, running=False)
    scan = _scan(client, kind="container", container="sknnr")
    assert scan["config_path"] == "/home/steam/enshrouded/enshrouded_server.json"
    assert scan["save_path"] == "/home/steam/enshrouded/savegame"
    assert scan["summary"]["save_format"] == "pre-1.0"
    assert scan["summary"]["legacy_password"] is True
    assert scan["port"] == 24000  # the host port players connect to
    world = _import(client, scan["token"], query_port=scan["port"])
    assert (_new_save(settings, world["slug"]) / "3ad85aea").read_bytes() == b"pre-1.0 world"
    doc = _live_config(settings, world["slug"])
    assert [(r["name"], r["password"]) for r in doc["userGroups"]] == [("default", "letmein")]
    assert "password" not in doc and doc["queryPort"] == 24000


def test_import_from_host_folder(
    client: TestClient, fake_docker: FakeDockerAdapter, settings: Settings, tmp_path: Path
) -> None:
    server = tmp_path / "native" / "EnshroudedServer"
    (server / "savegame").mkdir(parents=True)
    (server / "savegame" / "world0.ksd").write_bytes(b"native world")
    (server / "enshrouded_server.json").write_text(json.dumps(CONFIG))
    assert client.post("/api/imports/scan",
                       json={"kind": "host_path", "path": "relative"}).status_code == 422
    scan = _scan(client, kind="host_path", path=str(tmp_path / "native"))
    assert scan["save_path"] == f"{server}/savegame"
    assert scan["config_path"] == f"{server}/enshrouded_server.json"
    assert scan["summary"]["name"] == "Old Embervale" and scan["reusable_port"] is None
    world = _import(client, scan["token"])
    assert (_new_save(settings, world["slug"]) / "world0.ksd").read_bytes() == b"native world"
    assert _live_config(settings, world["slug"])["userGroups"][0]["password"] == "AdminPw1"
    # The folder was only read.
    assert (server / "savegame" / "world0.ksd").exists()


def test_host_folder_without_saves(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    res = client.post("/api/imports/scan", json={"kind": "host_path",
                                                 "path": str(tmp_path / "empty")})
    assert res.status_code == 422 and "No Enshrouded savegame" in res.json()["detail"]


def test_import_uploaded_zip(
    client: TestClient, settings: Settings
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("enshrouded_server.json", json.dumps(CONFIG))
        z.writestr("savegame/world0.ksd", b"zipped world")
    res = client.post("/api/imports/upload",
                      files={"file": ("server.zip", buf.getvalue(), "application/zip")})
    assert res.status_code == 200, res.text
    scan = res.json()
    assert scan["source"] == {"kind": "upload", "filename": "server.zip", "label": "server.zip"}
    world = _import(client, scan["token"])
    assert (_new_save(settings, world["slug"]) / "world0.ksd").read_bytes() == b"zipped world"

    res = client.post("/api/imports/upload",
                      files={"file": ("bad.zip", b"not a zip", "application/zip")})
    assert res.status_code == 422


def test_config_problems_block_the_import(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    bad = {**CONFIG, "userGroups": [ADMIN, {**ADMIN, "name": "Twin"}]}
    _mornedhels(tmp_path, fake_docker, running=False, config=bad)
    scan = _scan(client, kind="container", container="enshrouded")
    assert scan["errors"] and "Same password" in scan["errors"][0]
    res = client.post(f"/api/imports/{scan['token']}/create",
                      json={"display_name": "Embervale"})
    assert res.status_code == 422


def test_refuses_managed_and_auto_remove_containers(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    _mornedhels(tmp_path, fake_docker, auto_remove=True)
    scan = _scan(client, kind="container", container="enshrouded")
    res = client.post(f"/api/imports/{scan['token']}/create",
                      json={"display_name": "Embervale", "confirm_players": True})
    assert res.status_code == 409 and "--rm" in res.json()["detail"]

    res = client.post("/api/worlds", json={"display_name": "Managed"})
    wait_job(client, res.json()["job_id"])
    res = client.post("/api/imports/scan",
                      json={"kind": "container", "container": "flamekeeper-managed"})
    assert res.status_code == 409
    assert client.get("/api/imports/0123456789abcdef").status_code == 404
    assert client.get("/api/imports/..%2F..").status_code == 404


@pytest.mark.parametrize(("slots", "expected"), [(32, 16), (4, 4)])
def test_sanitize_clamps_slots(slots: int, expected: int) -> None:
    doc, warnings, errors = sanitize({**CONFIG, "slotCount": slots})
    assert doc["slotCount"] == expected and not errors
    assert any("16" in w for w in warnings) == (slots > 16)


def test_restart_during_import_puts_the_old_server_back(
    client: TestClient, fake_docker: FakeDockerAdapter, tmp_path: Path
) -> None:
    from app.db import session_scope
    from app.models import WorldImport

    _mornedhels(tmp_path, fake_docker)
    scan = _scan(client, kind="container", container="enshrouded")
    world = _import(client, scan["token"], query_port=15637, confirm_players=True)
    with session_scope() as s:  # as if the controller died mid-import
        s.get(WorldImport, world["id"]).state = "importing"
    services = client.app.state.services
    client.portal.call(services.imports.recover_interrupted)
    old = fake_docker._find("enshrouded")
    assert old.status == "running" and old.restart_policy == "unless-stopped"
    assert fake_docker._find(f"flamekeeper-{world['slug']}").status == "exited"
