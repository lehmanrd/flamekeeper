#!/usr/bin/env python3
"""Run a real Enshrouded server through an installed FlameKeeper, using its API.

Creates a world with the defaults (Steam download, image's default tag), waits until
the server answers queries, checks Keen's settings readme against FlameKeeper's
schema, then backs up (stopping the server so it saves), restores that backup,
restarts, and stops. Exits non-zero on the first problem.

    python3 scripts/ci/game_server_test.py --url http://localhost:8080 \\
        --user keeper --password secret
"""

from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile

FIRST_ONLINE_TIMEOUT_S = 60 * 60  # includes the ~9 GB game download
ONLINE_TIMEOUT_S = 15 * 60
JOB_TIMEOUT_S = 30 * 60
CRASHED_POLLS = 6  # "crashed" this many polls in a row fails the run
POLL_S = 10


class Failure(Exception):
    pass


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/") + "/api"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: object = None, raw: bool = False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-FlameKeeper": "1", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=60) as res:
                content = res.read()
        except urllib.error.HTTPError as e:
            raise Failure(f"{method} {path}: HTTP {e.code} {e.read().decode()[:500]}") from e
        if raw:
            return content
        return json.loads(content) if content else None

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: object = None):
        return self.call("POST", path, body if body is not None else {})


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_job(api: Api, job_id: str, what: str) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        job = api.get(f"/jobs/{job_id}")
        if job.get("message") != last:
            last = job.get("message")
            log(f"   {what}: {job['status']} {last or ''}")
        if job["status"] == "succeeded":
            return
        if job["status"] in ("failed", "cancelled"):
            raise Failure(f"{what} job {job['status']}: {job.get('message')}\n"
                          f"{json.dumps(job, indent=2)[:4000]}")
        time.sleep(POLL_S)
    raise Failure(f"{what} job didn't finish within {JOB_TIMEOUT_S // 60} minutes")


def wait_state(api: Api, world_id: str, want: str, timeout_s: int) -> dict:
    deadline = time.monotonic() + timeout_s
    crashed = 0
    last = None
    while time.monotonic() < deadline:
        w = api.get(f"/worlds/{world_id}")
        progress = w.get("update_progress")
        line = f"{w['state']} {json.dumps(progress) if progress else ''}".strip()
        if line != last:
            last = line
            log(f"   state: {line}")
        if w["state"] == want:
            return w
        crashed = crashed + 1 if w["state"] == "crashed" else 0
        if crashed >= CRASHED_POLLS:
            raise Failure(f"the server keeps crashing (last failure: {w.get('last_failure')})")
        time.sleep(POLL_S)
    raise Failure(f"the server wasn't {want} within {timeout_s // 60} minutes "
                  f"(state {last})")


def run(api: Api, args: argparse.Namespace) -> None:
    api.post("/auth/login", {"username": args.user, "password": args.password})
    system = api.get("/system")
    log(f"FlameKeeper {system['version']}, Docker {system['docker_version']}")
    bad = [c for c in system["selfcheck"]["checks"] if c.get("status") == "fail"]
    if bad:
        raise Failure(f"self-check failed: {bad}")

    log("== create a world")
    body: dict = {"display_name": "CI Test World", "slot_count": 4}
    if args.image_tag:
        body["image_tag"] = args.image_tag
    created = api.post("/worlds", body)
    world = created["world"]
    wid = world["id"]
    log(f"   {world['slug']}: image {world['image']}, port {world['query_port']}")
    wait_job(api, created["job_id"], "create")

    log("== wait until the server answers queries (downloads the game first)")
    w = wait_state(api, wid, "online", FIRST_ONLINE_TIMEOUT_S)
    log(f"   online: server {w['server_version']}, build {w['build_id']}, "
        f"players {w['players']}/{w['max_players']}")
    if w["max_players"] != 4 and system["docker_version"] != "fake":
        raise Failure(f"the server reports {w['max_players']} slots, not the 4 we set")

    log("== Keen's settings readme vs FlameKeeper's schema")
    readme = api.get("/system/settings-readme")
    for r in readme["readmes"]:
        log(f"   readme {r['version']}: {r['settings']} settings, {len(r['drift'])} differences")
        for d in r["drift"]:
            log(f"     - {d.get('message', d)}")
    if not readme["readmes"] and system["docker_version"] != "fake":
        raise Failure("the game install has no enshrouded_server_readme.txt")
    if any(r["drift"] for r in readme["readmes"]):
        raise Failure("the game's settings changed; update settings-schema.json")

    log("== back up (stops the server so it saves, then starts it)")
    job = api.post(f"/worlds/{wid}/backups", {"note": "CI", "mode": "stop"})
    wait_job(api, job["job_id"], "backup")
    backup = next(b for b in api.get(f"/worlds/{wid}/backups")["backups"] if b["note"] == "CI")
    content = api.call("GET", f"/worlds/{wid}/backups/{backup['id']}/download", raw=True)
    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    saves = [n for n in names if n.startswith("savegame/") and not n.endswith("/")]
    log(f"   {backup['filename']}: {len(content)} bytes, {len(saves)} save files")
    for n in names:
        log(f"     {n}")
    if not saves:
        raise Failure("the backup has no save files")
    wait_state(api, wid, "online", ONLINE_TIMEOUT_S)

    log("== restore that backup")
    job = api.post(f"/worlds/{wid}/backups/{backup['id']}/restore", {})
    wait_job(api, job["job_id"], "restore")
    wait_state(api, wid, "online", ONLINE_TIMEOUT_S)

    log("== restart the container")
    job = api.post(f"/worlds/{wid}/restart", {"mode": "container"})
    wait_job(api, job["job_id"], "restart")
    wait_state(api, wid, "online", ONLINE_TIMEOUT_S)

    log("== stop")
    job = api.post(f"/worlds/{wid}/stop", {})
    wait_job(api, job["job_id"], "stop")
    wait_state(api, wid, "stopped", 5 * 60)

    events = api.get(f"/worlds/{wid}/events")
    log("== events")
    for e in reversed(events):
        log(f"   {e.get('ts', '')} {e['kind']} {json.dumps(e.get('data') or {})[:200]}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--image-tag", default="", help="game image tag (default: FlameKeeper's)")
    args = p.parse_args()
    try:
        run(Api(args.url), args)
    except Failure as e:
        log(f"FAIL: {e}")
        return 1
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
