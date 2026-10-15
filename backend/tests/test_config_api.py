"""Config editor, roles, revisions, import/export and container settings."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.docker_fake import FakeDockerAdapter
from tests.conftest import FIXTURES, wait_for, wait_job
from tests.test_lifecycle import _create


def _config_path(settings: Settings, slug: str = "embervale") -> Path:
    return settings.worlds_dir / slug / "game" / "server" / "enshrouded_server.json"


def _file(settings: Settings) -> dict:
    return json.loads(_config_path(settings).read_text())


def _saved(client: TestClient, wid: str) -> dict:
    """The config FlameKeeper has saved: the live file plus any pending revision."""
    return client.get(f"/api/worlds/{wid}/config/export?include_passwords=true").json()


def _put(client: TestClient, wid: str, **body):
    rev = client.get(f"/api/worlds/{wid}/config").json()["revision"]
    return client.put(f"/api/worlds/{wid}/config", json={"base_revision": rev, **body})


def _keep_roles(cfg: dict) -> list[dict]:
    """Roles as the editor sends them back unchanged: passwords kept by index."""
    return [{**{k: v for k, v in r.items() if k not in ("index", "has_password")},
             "keep_password_from": r["index"]} for r in cfg["roles"]]


def test_enemy_damage_200_acceptance(client: TestClient, settings: Settings,
                                     fake_docker: FakeDockerAdapter) -> None:
    """Enemy Damage 200% + apply → Custom preset and factor 2 in the JSON."""
    world = _create(client)
    wid = world["id"]
    before = _file(settings)

    res = _put(client, wid, preset="Custom", gameSettings={"enemyDamageFactor": 2})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["revision"] == 2
    assert [c["label"] for c in body["changes"]] == ["Preset", "Enemy Damage"]
    doc = _saved(client, wid)
    assert doc["gameSettingsPreset"] == "Custom"
    assert doc["gameSettings"]["enemyDamageFactor"] == 2
    # Only those two values changed; tags, bans and roles are untouched.
    assert {k: v for k, v in doc.items() if k not in ("gameSettingsPreset", "gameSettings")} == \
        {k: v for k, v in before.items() if k not in ("gameSettingsPreset", "gameSettings")}

    summary = client.get(f"/api/worlds/{wid}").json()
    assert summary["pending_restart"] and summary["config_revision"] == 2
    view = client.get(f"/api/worlds/{wid}/config").json()
    assert view["preset"] == "Custom" and view["applied"]["preset"] == before["gameSettingsPreset"]
    assert view["gameSettings"]["enemyDamageFactor"] == 2
    assert view["applied"]["gameSettings"]["enemyDamageFactor"] != 2
    # Staged while the server runs: the live file changes only at the restart.
    assert _file(settings) == before

    job_id = client.post(f"/api/worlds/{wid}/config/apply", json={}).json()["job_id"]
    assert wait_job(client, job_id)["status"] == "succeeded"
    summary = client.get(f"/api/worlds/{wid}").json()
    assert not summary["pending_restart"] and summary["applied_revision"] == 2
    view = client.get(f"/api/worlds/{wid}/config").json()
    assert view["applied"] == {"preset": "Custom", "gameSettings": view["gameSettings"]}
    live = _file(settings)
    assert (live["gameSettingsPreset"], live["gameSettings"]["enemyDamageFactor"]) == ("Custom", 2)
    assert [c for c in fake_docker.exec_calls if c[1][0] == "supervisorctl"][-2:] == [
        ("flamekeeper-embervale", ["supervisorctl", "stop", "enshrouded-server"]),
        ("flamekeeper-embervale", ["supervisorctl", "start", "enshrouded-server"])]
    kinds = [e["kind"] for e in client.get(f"/api/worlds/{wid}/events").json()]
    assert "config_saved" in kinds and "config_applied" in kinds


def test_reference_file_round_trip_through_api(client: TestClient, settings: Settings) -> None:
    """With the shipped file in place, one edit changes only that value
    (plus the preset); tags and bannedAccounts survive every save."""
    world = _create(client)
    wait_job(client, client.post(f"/api/worlds/{world['id']}/stop").json()["job_id"])
    path = _config_path(settings)
    ref = json.loads((FIXTURES / "reference-default-enshrouded_server.json").read_text())
    ref["tags"] = ["English"]
    ref["bannedAccounts"] = ["76561190000000000"]
    path.write_text(json.dumps(ref, indent="\t"))
    raw_before = path.read_text()

    cfg = client.get(f"/api/worlds/{world['id']}/config").json()
    res = _put(client, world["id"], preset="Custom", gameSettings={"enemyDamageFactor": 2},
               roles=_keep_roles(cfg), server={"name": cfg["server"]["name"]})
    assert res.status_code == 200, res.text

    before, after = json.loads(raw_before), _file(settings)
    assert after["tags"] == ["English"] and after["bannedAccounts"] == ["76561190000000000"]
    assert list(after) == list(before)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"gameSettingsPreset", "gameSettings"}
    assert {k for k in before["gameSettings"]
            if before["gameSettings"][k] != after["gameSettings"][k]} == {"enemyDamageFactor"}
    # Tabs, like the game's own file.
    assert "\n\t\"name\"" in path.read_text()


def test_validation_errors(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    before = _config_path(settings).read_text()

    res = _put(client, wid, gameSettings={"curseModifier": "Off"})
    assert res.status_code == 422
    problem = res.json()
    assert problem["errors"][0]["loc"] == ["gameSettings", "curseModifier"]
    assert "Easy" in problem["detail"]

    res = _put(client, wid, gameSettings={"enemyDamageFactor": 9, "madeUpSetting": 1,
                                          "enableDurability": "yes"})
    locs = {tuple(e["loc"]) for e in res.json()["errors"]}
    assert locs == {("gameSettings", "enemyDamageFactor"), ("gameSettings", "madeUpSetting"),
                    ("gameSettings", "enableDurability")}

    assert _put(client, wid, preset="Nightmare").status_code == 422
    assert _put(client, wid, server={"tags": ["probe-tag"]}).status_code == 422
    assert _put(client, wid, server={"slotCount": 17}).status_code == 422
    assert _put(client, wid, server={"queryPort": 15999}).status_code == 422
    assert _config_path(settings).read_text() == before

    stale = client.put(f"/api/worlds/{wid}/config",
                       json={"base_revision": 0, "gameSettings": {"enemyDamageFactor": 2}})
    assert stale.status_code == 409


def test_no_op_save_keeps_revision(client: TestClient) -> None:
    world = _create(client)
    res = _put(client, world["id"], preset="Default")
    assert res.status_code == 200 and res.json()["revision"] == 1
    assert res.json()["changes"] == []


def test_roles_editing(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    cfg = client.get(f"/api/worlds/{wid}/config").json()
    original = {r["name"]: r["password"] for r in _file(settings)["userGroups"]}

    roles = _keep_roles(cfg)
    # Reorder, rename, change a permission, open the Visitor role, add a new role.
    roles = [roles[1], roles[0], roles[2], roles[3]]
    roles[0]["name"] = "Friends"
    roles[2]["canEditBase"] = True
    roles[3] = {**roles[3], "password": ""}
    del roles[3]["keep_password_from"]
    roles.append({"name": "Builder", "password": "stone-ember-otter-moss", "canEditBase": True,
                  "reservedSlots": 2})
    res = _put(client, wid, roles=roles)
    assert res.status_code == 200, res.text
    assert res.json()["warnings"][0]["msg"].startswith("Open: anyone can join as Visitor")
    groups = _saved(client, wid)["userGroups"]
    assert [g["name"] for g in groups] == ["Friends", "Admin", "Guest", "Visitor", "Builder"]
    assert groups[0]["password"] == original["Friend"]
    assert groups[1]["password"] == original["Admin"]
    assert groups[2]["canEditBase"] is True
    assert groups[3]["password"] == ""
    assert groups[4] == {"name": "Builder", "password": "stone-ember-otter-moss",
                         "canKickBan": False, "canAccessInventories": False,
                         "canEditWorld": False, "canEditBase": True, "canExtendBase": False,
                         "reservedSlots": 2}
    labels = [c["label"] for c in res.json()["changes"]]
    assert "Role Builder added" in labels and "Role Friend removed" in labels
    assert all(c["before"] is None and c["after"] is None
               for c in res.json()["changes"] if c.get("secret"))
    assert "stone-ember" not in res.text

    view = client.get(f"/api/worlds/{wid}/config").json()
    assert [r["has_password"] for r in view["roles"]] == [True, True, True, False, True]
    assert "stone-ember" not in json.dumps(view)
    reveal = client.post(f"/api/worlds/{wid}/roles/4/reveal").json()
    assert reveal == {"index": 4, "name": "Builder", "password": "stone-ember-otter-moss"}
    assert client.post(f"/api/worlds/{wid}/roles/9/reveal").status_code == 404


def test_role_validation(client: TestClient) -> None:
    world = _create(client, slot_count=4)
    wid = world["id"]
    cfg = client.get(f"/api/worlds/{wid}/config").json()

    def errors(roles):
        res = _put(client, wid, roles=roles)
        assert res.status_code == 422, res.text
        return {(tuple(e["loc"]), e["msg"].split(":")[0]) for e in res.json()["errors"]}

    base = _keep_roles(cfg)
    dup_name = [*base, {"name": "admin", "password": "x1"}]
    assert ((("roles", 4, "name"), "Another role is already named 'admin'") in
            errors(dup_name))
    dup_pw = [{**base[0]}, {"name": "Copy", "password": "same"},
              {"name": "Copy2", "password": "same"}]
    assert any(loc == ("roles", 2, "password") for loc, _ in errors(dup_pw))
    two_open = [{"name": "A", "password": ""}, {"name": "B", "password": ""}]
    assert any(loc == ("roles", 1, "password") for loc, _ in errors(two_open))
    assert errors([]) == {(("roles",), "At least one role is required")}
    reserved = [{**base[0], "reservedSlots": 3}, {**base[1], "reservedSlots": 2}]
    assert any("Reserved slots add up to 5" in m for _, m in errors(reserved))
    assert any(loc == ("roles", 0, "password") for loc, _ in errors([{"name": "NoPw"}]))
    # Lowering max players below the reserved total is caught too.
    assert _put(client, wid, roles=[{**base[0], "reservedSlots": 3}]).status_code == 200
    res = _put(client, wid, server={"slotCount": 2})
    assert res.status_code == 422 and "Reserved slots add up to 3" in res.text


def test_server_fields_and_display_name(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    res = _put(client, world["id"], server={"name": "Ember Vale", "slotCount": 6,
                                            "tags": ["English", "BaseBuilding"],
                                            "enableVoiceChat": True, "voiceChatMode": "Global"})
    assert res.status_code == 200, res.text
    doc = _saved(client, world["id"])
    assert (doc["name"], doc["slotCount"], doc["tags"], doc["voiceChatMode"]) == \
        ("Ember Vale", 6, ["English", "BaseBuilding"], "Global")
    assert client.get(f"/api/worlds/{world['id']}").json()["display_name"] == "Ember Vale"


def test_revisions_diff_and_restore(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    _put(client, wid, preset="Custom", gameSettings={"enemyDamageFactor": 2})
    cfg = client.get(f"/api/worlds/{wid}/config").json()
    roles = _keep_roles(cfg)
    roles[0] = {k: v for k, v in roles[0].items() if k != "keep_password_from"}
    roles[0]["password"] = "new-admin-secret"
    _put(client, wid, roles=roles)

    revs = client.get(f"/api/worlds/{wid}/config/revisions").json()
    assert [r["revision"] for r in revs] == [3, 2, 1]
    assert revs[0]["current"] and revs[2]["applied"] and revs[2]["source"] == "created"

    rev3 = client.get(f"/api/worlds/{wid}/config/revisions/3").json()
    assert [c["label"] for c in rev3["changes"]] == ["Admin: Password"]
    assert "new-admin-secret" not in json.dumps(rev3)
    assert rev3["document"]["userGroups"][0]["password"] == "********"

    rev1 = client.get(f"/api/worlds/{wid}/config/revisions/1").json()
    assert {c["label"] for c in rev1["restore_changes"]} == {"Preset", "Enemy Damage",
                                                             "Admin: Password"}
    res = client.post(f"/api/worlds/{wid}/config/revisions/1/restore",
                      json={"base_revision": 3})
    assert res.status_code == 200, res.text
    assert res.json()["revision"] == 4
    doc = _file(settings)
    assert doc["gameSettingsPreset"] == "Default" and doc["gameSettings"]["enemyDamageFactor"] == 1
    assert doc["userGroups"][0]["password"] != "new-admin-secret"
    assert client.get(f"/api/worlds/{wid}/config/revisions").json()[0]["source"] == "restore"
    history = settings.worlds_dir / "embervale" / "config-history" / "4.json"
    assert history.stat().st_mode & 0o077 == 0
    assert client.get(f"/api/worlds/{wid}/config/revisions/99").status_code == 404


def test_export_and_import(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    masked = client.get(f"/api/worlds/{wid}/config/export")
    assert "attachment" in masked.headers["content-disposition"]
    exported = masked.json()
    assert {r["password"] for r in exported["userGroups"]} == {"********"}
    full = client.get(f"/api/worlds/{wid}/config/export?include_passwords=true").json()
    assert full == _file(settings)

    # Edit the masked export and import it: masked passwords are kept.
    exported["gameSettingsPreset"] = "Custom"
    exported["gameSettings"]["playerHealthFactor"] = 2
    exported["gameSettings"]["notARealSetting"] = 5
    exported["queryPort"] = 16000
    exported["userGroups"][3]["canEditWorld"] = True
    dry = client.post(f"/api/worlds/{wid}/config/import", json={"document": exported})
    assert dry.status_code == 200, dry.text
    assert {c["label"] for c in dry.json()["changes"]} == {"Preset", "Player Health",
                                                           "Visitor: Edit world"}
    warnings = " ".join(w["msg"] for w in dry.json()["warnings"])
    assert "notARealSetting" in warnings and "queryPort" in warnings
    assert _file(settings) == full  # dry run wrote nothing

    res = client.post(f"/api/worlds/{wid}/config/import?dry_run=false",
                      json={"document": exported, "base_revision": 1})
    assert res.status_code == 200, res.text
    doc = _saved(client, wid)
    assert doc["gameSettings"]["playerHealthFactor"] == 2
    assert [r["password"] for r in doc["userGroups"]] == [r["password"] for r in full["userGroups"]]
    assert doc["queryPort"] == full["queryPort"]

    bad = {**exported, "userGroups": [{"name": "Stranger", "password": "********"}]}
    res = client.post(f"/api/worlds/{wid}/config/import", json={"document": bad})
    assert res.status_code == 422 and "masked password" in res.text
    res = client.post(f"/api/worlds/{wid}/config/import",
                      json={"document": {**exported, "gameSettings": {"curseModifier": "Off"}}})
    assert res.status_code == 422


def test_legacy_password(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    path = _config_path(settings)
    doc = _saved(client, wid)
    doc["password"] = "oldsharedpw"
    path.write_text(json.dumps(doc, indent="\t"))
    view = client.get(f"/api/worlds/{wid}/config").json()
    assert view["legacy_password"] is True
    assert "oldsharedpw" not in json.dumps(view)

    res = client.post(f"/api/worlds/{wid}/config/legacy-password/convert", json={})
    assert res.status_code == 200, res.text
    doc = _saved(client, wid)
    assert "password" not in doc
    assert doc["userGroups"][-1]["name"] == "default"
    assert doc["userGroups"][-1]["password"] == "oldsharedpw"
    assert doc["userGroups"][-1]["canEditBase"] is True
    assert client.post(f"/api/worlds/{wid}/config/legacy-password/convert").status_code == 409


def test_apply_needs_running_world(client: TestClient) -> None:
    world = _create(client)
    wait_job(client, client.post(f"/api/worlds/{world['id']}/stop").json()["job_id"])
    res = client.post(f"/api/worlds/{world['id']}/config/apply", json={})
    assert res.status_code == 409


def test_infra_patch_and_recreate(client: TestClient, settings: Settings,
                                  fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    other = _create(client, display_name="Second", query_port=15700)
    wid = world["id"]
    assert client.get(f"/api/worlds/{wid}").json()["recreate_required"] is False

    res = client.patch(f"/api/worlds/{wid}", json={"query_port": 15700})
    assert res.status_code == 409
    res = client.patch(f"/api/worlds/{wid}", json={"mem_limit_mb": 10})
    assert res.status_code == 422
    res = client.patch(f"/api/worlds/{wid}", json={"bind_ip": "not-an-ip"})
    assert res.status_code == 422
    res = client.patch(f"/api/worlds/{wid}", json={"nonsense": 1})
    assert res.status_code == 422

    res = client.patch(f"/api/worlds/{wid}", json={"query_port": 15650, "cpu_limit": 4,
                                                   "image_tag": "stable-wine"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changed"] == ["cpu_limit", "query_port"]
    assert body["recreate_required"] is True
    assert _saved(client, wid)["queryPort"] == 15650
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["recreate_required"])
    assert client.get(f"/api/worlds/{other['id']}").json()["recreate_required"] is False

    job = wait_job(client, client.post(f"/api/worlds/{wid}/recreate").json()["job_id"])
    assert job["status"] == "succeeded"
    spec = fake_docker.spec_of("flamekeeper-embervale")
    assert _file(settings)["queryPort"] == 15650
    assert spec.ports == {"15650/udp": ("0.0.0.0", 15650)}
    assert spec.env["SERVER_QUERYPORT"] == "15650"
    assert spec.nano_cpus == 4_000_000_000
    after = client.get(f"/api/worlds/{wid}").json()
    assert not after["recreate_required"] and not after["pending_restart"]

    # A stopped world picks up container changes when it starts.
    wait_job(client, client.post(f"/api/worlds/{wid}/stop").json()["job_id"])
    assert client.patch(f"/api/worlds/{wid}", json={"stop_timeout_s": 120}).status_code == 200
    job = wait_job(client, client.post(f"/api/worlds/{wid}/start").json()["job_id"])
    assert job["status"] == "succeeded"
    assert fake_docker.spec_of("flamekeeper-embervale").stop_timeout == 120
    assert "recreating the container" in client.get(f"/api/jobs/{job['id']}").json()["log"]


def _server_ban_write(settings: Settings, damage: float) -> None:
    """What the server does when a ban changes in game: rewrite the whole file from
    memory (old settings) with the new ban list."""
    doc = _file(settings)
    doc["gameSettings"]["enemyDamageFactor"] = damage
    doc["bannedAccounts"] = [{"accountId": 12345678901234567891,
                              "displayName": "Ashwalker", "characterName": "Emberling",
                              "banDate": {"value": 1792031497}}]
    _config_path(settings).write_text(json.dumps(doc, indent="\t"))


def test_pending_changes_survive_a_server_ban_write(client: TestClient, settings: Settings,
                                                    fake_docker: FakeDockerAdapter) -> None:
    """The running server rewrites its file from memory when a ban changes in game.
    Saves are staged and merged in only at the restart, keeping the server's bans."""
    world = _create(client)
    wid = world["id"]
    assert _put(client, wid, preset="Custom", gameSettings={"enemyDamageFactor": 1.5}) \
        .status_code == 200
    _server_ban_write(settings, damage=1.0)
    assert _file(settings)["gameSettings"]["enemyDamageFactor"] == 1.0
    view = client.get(f"/api/worlds/{wid}/config").json()
    assert view["gameSettings"]["enemyDamageFactor"] == 1.5  # editors show what's saved
    saved = _saved(client, wid)
    assert saved["gameSettings"]["enemyDamageFactor"] == 1.5
    assert saved["bannedAccounts"][0]["displayName"] == "Ashwalker"

    job = client.post(f"/api/worlds/{wid}/config/apply", json={}).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded"
    doc = _file(settings)
    assert doc["gameSettings"]["enemyDamageFactor"] == 1.5
    assert doc["gameSettingsPreset"] == "Custom"
    assert doc["bannedAccounts"][0]["accountId"] == 12345678901234567891  # exact, kept
    assert "Applied saved config changes: Preset, Enemy Damage" in \
        client.get(f"/api/jobs/{job}").json()["log"]

    # Once applied, the server's writes are the truth: nothing pending to merge.
    _server_ban_write(settings, damage=1.5)
    assert client.app.state.services.config.apply_pending(
        client.app.state.services.worlds.get(wid)) == []


def test_stopped_world_gets_changes_immediately(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    wait_job(client, client.post(f"/api/worlds/{wid}/stop").json()["job_id"])
    assert _put(client, wid, gameSettings={"enemyDamageFactor": 3}).status_code == 200
    assert _file(settings)["gameSettings"]["enemyDamageFactor"] == 3
