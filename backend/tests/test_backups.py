"""Backup archives, backups API, restore, retention, schedules and clone."""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import session_scope
from app.models import Backup, PlayerSession, Schedule
from app.services import backup_archive as archive
from app.services.backup_service import next_runs, validate_cron
from app.services.docker_fake import FakeDockerAdapter
from app.services.world_service import WorldError
from tests.conftest import wait_for, wait_job
from tests.test_lifecycle import _create


def _save_dir(settings: Settings, slug: str = "embervale") -> Path:
    return settings.worlds_dir / slug / "game" / "server" / "savegame"


def _zip(entries: dict[str, bytes], path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


# -- archive ---------------------------------------------------------------------------


def test_write_inspect_extract_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "savegame"
    src.mkdir()
    (src / "3ad85aea").write_bytes(b"world")
    (src / "3ad85aea-1").write_bytes(b"older")
    (src / "3ad85aea-index").write_text("{}")
    staged = tmp_path / "staged"
    archive.snapshot_dir(src, staged)
    dest = tmp_path / "b.zip"
    archive.write_backup(dest, staged, b'{"name": "x"}', {"kind": "manual"})
    assert dest.stat().st_mode & 0o077 == 0

    s = archive.inspect_zip(dest)
    assert s.save_prefix == "savegame" and s.has_config and s.manifest["kind"] == "manual"
    assert s.manifest["files"][0] == {"name": "savegame/3ad85aea", "size": 5}
    assert not s.warnings

    target = tmp_path / "restore" / "savegame"
    target.mkdir(parents=True)
    target.chmod(0o755)
    (target / "stale-file").write_text("gone after restore")
    archive.extract_saves(dest, s, target)
    assert sorted(p.name for p in target.iterdir()) == ["3ad85aea", "3ad85aea-1", "3ad85aea-index"]
    assert (target / "3ad85aea").read_bytes() == b"world"
    assert not (target.parent / "savegame.old").exists()
    assert target.stat().st_mode & 0o777 == 0o755
    assert archive.read_config(dest) == {"name": "x"}


def test_inspect_accepts_flat_and_nested_layouts(tmp_path: Path) -> None:
    flat = _zip({"3ad85aea": b"w", "3ad85aea-index": b"{}"}, tmp_path / "flat.zip")
    assert archive.inspect_zip(flat).save_prefix == ""
    nested = _zip({"MyWorld/savegame/3ad85aea": b"w"}, tmp_path / "nested.zip")
    s = archive.inspect_zip(nested)
    assert s.save_prefix == "MyWorld/savegame"
    assert s.warnings == ["No -index file: the server may not find the latest save"]


@pytest.mark.parametrize("entries,error", [
    ({"../evil": b"x", "3ad85aea": b"w"}, "Unsafe path"),
    ({"/etc/passwd": b"x"}, "Unsafe path"),
    ({"readme.txt": b"hello"}, "No Enshrouded save files"),
    ({"a/3ad85aea": b"w", "b/3ad85aea": b"w"}, "more than one folder"),
])
def test_inspect_rejects_bad_zips(tmp_path: Path, entries: dict, error: str) -> None:
    with pytest.raises(archive.ArchiveError, match=error):
        archive.inspect_zip(_zip(entries, tmp_path / "bad.zip"))


def test_inspect_rejects_non_zip_symlink_and_bomb(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(archive.ArchiveError, match="Not a valid zip"):
        archive.inspect_zip(junk)

    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as z:
        info = zipfile.ZipInfo("3ad85aea")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(archive.ArchiveError, match="symlink"):
        archive.inspect_zip(link)

    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("3ad85aea", b"\0" * (20 * 1024**2))
    with pytest.raises(archive.ArchiveError, match="compression ratio"):
        archive.inspect_zip(bomb)
    assert archive.inspect_zip(bomb, trusted=True).save_files == ["3ad85aea"]


def test_snapshot_retries_while_files_change(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "save"
    src.mkdir()
    (src / "3ad85aea").write_bytes(b"v1")
    calls = {"n": 0}
    real_copy = archive.shutil.copy2

    def copy_and_mutate(a, b):
        real_copy(a, b)
        calls["n"] += 1
        if calls["n"] == 1:  # the server saves during the first attempt
            (src / "3ad85aea").write_bytes(b"version two")
            os.utime(src / "3ad85aea", ns=(1, 2_000_000_000))

    monkeypatch.setattr(archive.shutil, "copy2", copy_and_mutate)
    archive.snapshot_dir(src, tmp_path / "out")
    assert (tmp_path / "out" / "3ad85aea").read_bytes() == b"version two"
    assert calls["n"] == 2


# -- cron ------------------------------------------------------------------------------


def test_cron_validation() -> None:
    validate_cron("0 * * * *")
    validate_cron("*/15 * * * *")
    with pytest.raises(WorldError, match="every 10 minutes"):
        validate_cron("*/5 * * * *")
    with pytest.raises(WorldError, match="Invalid cron"):
        validate_cron("every hour")
    runs = next_runs("30 4 * * *", datetime(2026, 10, 15, 12, 0, tzinfo=UTC), 2)
    assert runs[1] - runs[0] == timedelta(days=1)


# -- API -------------------------------------------------------------------------------


def _backup(client: TestClient, wid: str, **body) -> dict:
    body.setdefault("mode", "last_save")
    job = client.post(f"/api/worlds/{wid}/backups", json=body).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded", client.get(f"/api/jobs/{job}").json()
    return client.get(f"/api/worlds/{wid}/backups").json()["backups"][0]


def test_manual_backup_list_download_note_delete(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    listing = client.get(f"/api/worlds/{wid}/backups").json()
    assert listing["backups"] == []
    assert listing["schedule"]["cron"] == "0 * * * *" and listing["schedule"]["enabled"]
    assert listing["disk"]["savegame_bytes"] > 0

    b = _backup(client, wid, note="Before the raid")
    assert (b["kind"], b["note"]) == ("manual", "Before the raid")
    assert b["filename"].endswith("_manual.zip") and b["size_bytes"] > 0

    res = client.get(f"/api/worlds/{wid}/backups/{b['id']}/download")
    assert res.status_code == 200
    assert res.headers["content-disposition"].startswith('attachment; filename="embervale-')
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        names = z.namelist()
        manifest = json.loads(z.read("flamekeeper-backup.json"))
    assert "enshrouded_server.json" in names and "savegame/3ad85aea" in names
    assert manifest["world"]["slug"] == "embervale" and manifest["kind"] == "manual"

    info = client.get(f"/api/worlds/{wid}/backups/{b['id']}").json()
    assert info["has_config"] and "savegame/3ad85aea" in info["save_files"]

    assert client.patch(f"/api/worlds/{wid}/backups/{b['id']}",
                        json={"note": "renamed"}).json()["note"] == "renamed"
    assert client.delete(f"/api/worlds/{wid}/backups/{b['id']}").status_code == 204
    assert client.get(f"/api/worlds/{wid}/backups").json()["backups"] == []
    assert client.get(f"/api/worlds/{wid}/backups/{b['id']}/download").status_code == 404
    kinds = [e["kind"] for e in client.get(f"/api/worlds/{wid}/events").json()]
    assert "backup_created" in kinds


def test_backup_with_stop_server_restarts_it(client: TestClient,
                                            fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    _backup(client, world["id"], mode="stop")
    assert ("stop", "flamekeeper-embervale") in fake_docker.calls
    assert wait_for(lambda: client.get(f"/api/worlds/{world['id']}").json()["state"] == "online")
    assert client.get(f"/api/worlds/{world['id']}").json()["desired_state"] == "running"


def test_restore_round_trip(client: TestClient, settings: Settings,
                            fake_docker: FakeDockerAdapter) -> None:
    """Build something, back up, destroy it, restore, it's back."""
    world = _create(client)
    wid = world["id"]
    save = _save_dir(settings)
    (save / "3ad85aea").write_bytes(b"world with a big castle")
    b = _backup(client, wid)

    (save / "3ad85aea").write_bytes(b"castle destroyed")
    (save / "3ad85aea-9").write_bytes(b"later save")
    res = client.post(f"/api/worlds/{wid}/backups/{b['id']}/restore", json={})
    job = wait_job(client, res.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert (save / "3ad85aea").read_bytes() == b"world with a big castle"
    assert not (save / "3ad85aea-9").exists()

    backups = client.get(f"/api/worlds/{wid}/backups").json()["backups"]
    pre = [x for x in backups if x["kind"] == "pre-restore"]
    assert len(pre) == 1 and b["filename"] in pre[0]["note"]
    with zipfile.ZipFile(settings.worlds_dir / "embervale" / pre[0]["path"]) as z:
        assert z.read("savegame/3ad85aea") == b"castle destroyed"
    assert wait_for(lambda: client.get(f"/api/worlds/{wid}").json()["state"] == "online")
    assert "restored" in [e["kind"] for e in client.get(f"/api/worlds/{wid}/events").json()]


def test_restore_with_config_and_while_stopped(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    b = _backup(client, wid)
    rev = client.get(f"/api/worlds/{wid}/config").json()["revision"]
    client.put(f"/api/worlds/{wid}/config", json={"base_revision": rev, "preset": "Hard"})
    wait_job(client, client.post(f"/api/worlds/{wid}/stop").json()["job_id"])

    res = client.post(f"/api/worlds/{wid}/backups/{b['id']}/restore",
                      json={"restore_config": True})
    assert wait_job(client, res.json()["job_id"])["status"] == "succeeded"
    assert client.get(f"/api/worlds/{wid}/config").json()["preset"] == "Default"
    revs = client.get(f"/api/worlds/{wid}/config/revisions").json()
    assert revs[0]["source"] == "backup"
    # It was stopped, so it stays stopped.
    assert client.get(f"/api/worlds/{wid}").json()["state"] == "stopped"


def test_upload(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    world = _create(client)
    wid = world["id"]
    good = _zip({"3ad85aea": b"uploaded world", "3ad85aea-index": b"{}"}, tmp_path / "w.zip")
    res = client.post(f"/api/worlds/{wid}/backups/upload",
                      files={"file": ("my-world.zip", good.read_bytes(), "application/zip")})
    assert res.status_code == 201, res.text
    b = res.json()
    assert b["kind"] == "uploaded" and b["note"] == "Uploaded my-world.zip"
    assert b["save_files"] == 2 and not b["has_config"]

    res = client.post(f"/api/worlds/{wid}/backups/{b['id']}/restore", json={"restore_config": True})
    assert res.status_code == 422 and "no enshrouded_server.json" in res.text
    job = client.post(f"/api/worlds/{wid}/backups/{b['id']}/restore", json={}).json()["job_id"]
    assert wait_job(client, job)["status"] == "succeeded"
    assert (_save_dir(settings) / "3ad85aea").read_bytes() == b"uploaded world"

    bad = _zip({"notes.txt": b"hi"}, tmp_path / "bad.zip")
    res = client.post(f"/api/worlds/{wid}/backups/upload",
                      files={"file": ("bad.zip", bad.read_bytes(), "application/zip")})
    assert res.status_code == 422 and "No Enshrouded save files" in res.text
    folder = settings.worlds_dir / "embervale" / "backups"
    assert not [p for p in folder.iterdir() if p.name.startswith(".upload-")]


def test_sync_picks_up_files_on_disk(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    root = settings.worlds_dir / "embervale"
    _zip({"3ad85aea": b"w"}, root / "backups" / "dropped-by-hand.zip")
    (root / "game" / "server" / "backups").mkdir()
    _zip({"3ad85aea": b"w"}, root / "game" / "server" / "backups" / "2026-10-01_12-00-00.zip")
    kinds = {b["filename"]: b["kind"] for b in client.get(f"/api/worlds/{wid}/backups")
             .json()["backups"]}
    assert kinds == {"dropped-by-hand.zip": "uploaded", "2026-10-01_12-00-00.zip": "image"}
    (root / "backups" / "dropped-by-hand.zip").unlink()
    assert len(client.get(f"/api/worlds/{wid}/backups").json()["backups"]) == 1


def test_retention(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    services = client.app.state.services
    w = services.worlds.get(wid)
    root = settings.worlds_dir / "embervale"
    old = datetime.now(UTC) - timedelta(days=40)
    with session_scope() as s:
        for i in range(5):
            name = f"backups/2026010{i}-000000_scheduled.zip"
            _zip({"3ad85aea": b"w"}, root / name)
            s.add(Backup(world_id=wid, filename=name, size_bytes=1, kind="scheduled",
                         created_at=old + timedelta(hours=i)))
        for kind in ("manual", "pre-restore"):
            name = f"backups/old_{kind}.zip"
            _zip({"3ad85aea": b"w"}, root / name)
            s.add(Backup(world_id=wid, filename=name, size_bytes=1, kind=kind, created_at=old))
    res = client.put(f"/api/worlds/{wid}/schedules/backup",
                     json={"retention_count": 2, "max_age_days": 30})
    assert res.status_code == 200, res.text
    left = sorted(b["filename"] for b in services.backups.list(w))
    assert left == ["20260103-000000_scheduled.zip", "20260104-000000_scheduled.zip"]
    assert not (root / "backups" / "old_manual.zip").exists()


def test_schedule_api_and_due_runs(client: TestClient, settings: Settings) -> None:
    world = _create(client)
    wid = world["id"]
    services = client.app.state.services
    res = client.put(f"/api/worlds/{wid}/schedules/backup", json={"cron": "*/5 * * * *"})
    assert res.status_code == 422
    res = client.put(f"/api/worlds/{wid}/schedules/backup", json={"cron": "bogus"})
    assert res.status_code == 422
    res = client.put(f"/api/worlds/{wid}/schedules/backup", json={"cron": "*/30 * * * *"})
    sched = res.json()
    assert sched["cron"] == "*/30 * * * *" and len(sched["upcoming"]) == 3

    w = services.worlds.get(wid)
    assert client.portal.call(services.backups.run_due, w) is None  # not due yet
    with session_scope() as s:
        s.get(Schedule, (wid, "backup")).last_run_at = datetime.now(UTC) - timedelta(hours=1)
    job = client.portal.call(services.backups.run_due, w)
    assert job and wait_job(client, job)["status"] == "succeeded"
    assert services.backups.list(w)[0]["kind"] == "scheduled"

    # Due again, but nobody played since that backup: skipped.
    with session_scope() as s:
        s.get(Schedule, (wid, "backup")).last_run_at = datetime.now(UTC) - timedelta(hours=1)
    assert client.portal.call(services.backups.run_due, w) is None
    # Someone played: it runs.
    with session_scope() as s:
        s.get(Schedule, (wid, "backup")).last_run_at = datetime.now(UTC) - timedelta(hours=1)
        s.add(PlayerSession(world_id=wid, player_name="Wanderer"))
    job = client.portal.call(services.backups.run_due, w)
    assert job and wait_job(client, job)["status"] == "succeeded"
    # Disabled: never.
    client.put(f"/api/worlds/{wid}/schedules/backup", json={"enabled": False})
    with session_scope() as s:
        s.get(Schedule, (wid, "backup")).last_run_at = datetime.now(UTC) - timedelta(hours=1)
    assert client.portal.call(services.backups.run_due, w) is None


def test_clone_from_backup_and_current(client: TestClient, settings: Settings,
                                       fake_docker: FakeDockerAdapter) -> None:
    world = _create(client)
    wid = world["id"]
    (_save_dir(settings) / "3ad85aea").write_bytes(b"original world")
    rev = client.get(f"/api/worlds/{wid}/config").json()["revision"]
    client.put(f"/api/worlds/{wid}/config", json={"base_revision": rev, "preset": "Survival"})
    b = _backup(client, wid)
    (_save_dir(settings) / "3ad85aea").write_bytes(b"changed later")

    res = client.post(f"/api/worlds/{wid}/clone",
                      json={"source": b["id"], "display_name": "Embervale Copy",
                            "query_port": 15700})
    assert res.status_code == 202, res.text
    copy = res.json()["world"]
    assert wait_job(client, res.json()["job_id"])["status"] == "succeeded"
    assert (copy["slug"], copy["query_port"]) == ("embervale-copy", 15700)
    assert (_save_dir(settings, "embervale-copy") / "3ad85aea").read_bytes() == b"original world"
    cfg = client.get(f"/api/worlds/{copy['id']}/config").json()
    assert cfg["preset"] == "Survival" and cfg["server"]["name"] == "Embervale Copy"
    assert cfg["server"]["queryPort"] == 15700
    src_pw = json.loads((_save_dir(settings).parent / "enshrouded_server.json").read_text())
    copy_pw = json.loads((_save_dir(settings, "embervale-copy").parent /
                          "enshrouded_server.json").read_text())
    assert [r["password"] for r in copy_pw["userGroups"]] == \
        [r["password"] for r in src_pw["userGroups"]]

    res = client.post(f"/api/worlds/{wid}/clone",
                      json={"source": "current", "display_name": "Live Copy"})
    assert wait_job(client, res.json()["job_id"])["status"] == "succeeded"
    assert (_save_dir(settings, "live-copy") / "3ad85aea").read_bytes() == b"changed later"
    jobs = client.get("/api/jobs").json()
    assert sum(1 for j in jobs if j["type"] == "clone") == 2
    assert client.post(f"/api/worlds/{wid}/clone", json={"source": "nope",
                                                       "display_name": "X"}).status_code == 404


def test_backup_waits_for_the_next_save(client: TestClient, settings: Settings,
                                       monkeypatch) -> None:
    """A running server only saves every ~5 minutes: by default "Back up now" waits for
    the next save so recent changes are in the backup."""
    import time

    from app.services import backup_service

    monkeypatch.setattr(backup_service, "SAVE_POLL_S", 0.05)
    monkeypatch.setattr(backup_service, "SAVE_SETTLE_S", 0.05)
    world = _create(client)
    wid = world["id"]
    save = _save_dir(settings)
    old = time.time() - 120
    for p in save.iterdir():
        os.utime(p, (old, old))
    job = client.post(f"/api/worlds/{wid}/backups", json={"note": "altar"}).json()["job_id"]
    wait_for(lambda: "next save" in client.get(f"/api/jobs/{job}").json()["message"])
    time.sleep(0.2)
    assert client.get(f"/api/jobs/{job}").json()["status"] == "running"
    (save / "3ad85aea").write_bytes(b"world with the flame altar")  # the autosave
    assert wait_job(client, job)["status"] == "succeeded"
    b = client.get(f"/api/worlds/{wid}/backups").json()["backups"][0]
    with zipfile.ZipFile(settings.worlds_dir / "embervale" / b["path"]) as z:
        assert z.read("savegame/3ad85aea") == b"world with the flame altar"
        assert json.loads(z.read("flamekeeper-backup.json"))["saved_at"] == b["saved_at"]
    assert datetime.fromisoformat(b["saved_at"]) > datetime.fromtimestamp(old, UTC)
    listing = client.get(f"/api/worlds/{wid}/backups").json()
    assert listing["savegame_saved_at"] == b["saved_at"]


def test_last_save_mode_records_the_older_save_time(client: TestClient,
                                                    settings: Settings) -> None:
    world = _create(client)
    save = _save_dir(settings)
    old = datetime(2026, 10, 15, 2, 3, 50, tzinfo=UTC).timestamp()
    for p in save.iterdir():
        os.utime(p, (old, old))
    b = _backup(client, world["id"])
    assert b["saved_at"] == "2026-10-15T02:03:50+00:00"
    assert b["created_at"] > b["saved_at"]
