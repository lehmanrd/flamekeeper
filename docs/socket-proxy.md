# Docker socket proxy

Status: **steps 1–5 done** (see Steps); installs run behind the proxy. This is the plan for taking the Docker socket
away from the FlameKeeper container.

## Why

FlameKeeper creates and manages the game containers through the host's Docker. Today the
host's `/var/run/docker.sock` is mounted into the FlameKeeper container, and anything that
can talk to that socket is root on the host: it can start a privileged container, bind
`/` read-write, join the host's PID namespace, and so on. FlameKeeper also uses some of
that power on purpose:

- the self-update runs a privileged container in the host PID namespace and `nsenter`s into
  PID 1 (`backend/app/services/app_update.py`);
- the importer binds the host's `/` read-only into a helper to find existing servers
  (`backend/app/services/import_service.py`);
- the ntsync check binds the host's `/dev` into a helper (`backend/app/services/selfcheck.py`).

So anyone who gets control of FlameKeeper (a bug in it, a leaked admin session) gets root
on the host.

### Why not Docker-in-Docker

DinD needs the outer container to be `--privileged`, which is just as root-equivalent as
the socket. Rootless DinD still needs it in most setups; Sysbox avoids it but has to be
installed on the host, which works against the one-line installer. DinD also ties the
game servers' lifetime to the FlameKeeper container: updating FlameKeeper would stop
every world. Game ports would still work (published twice: host → FlameKeeper → game),
but the range would have to be fixed when the FlameKeeper container is created.

## Design

```
                  (no network: unix sockets only)
┌──────────────┐  /run/fk/docker.sock  ┌────────────────┐  /var/run/docker.sock  ┌─────────┐
│ flamekeeper  │ ────────────────────▶ │ fk-dockerproxy │ ─────────────────────▶ │ dockerd │
│ (no socket)  │     shared volume     │  policy check  │                        └─────────┘
└──────────────┘                       └────────────────┘
```

- The proxy (`dockerproxy/`, Go) ships in the FlameKeeper image as `/usr/local/bin/fk-dockerproxy`
  and runs as its own compose service, so its policy always matches the app version.
- FlameKeeper gets `DOCKER_HOST=unix:///run/fk/docker.sock`; the Docker SDK picks it up.
- The proxy service: `network_mode: none`, read-only root filesystem, every capability
  dropped, `no-new-privileges`. It needs no access to `DATA_DIR`: every rule compares
  paths as text (see "World folders" below for why that's enough).
- Go rather than Python: exec start upgrades the HTTP connection, and logs/pull/stats are
  long streams; `httputil.ReverseProxy` handles both. URL-only filters like
  `tecnativa/docker-socket-proxy` don't help: the escape is in the body of
  `POST /containers/create`.

### Modes

`FK_PROXY_MODE=audit` (default) checks every request and logs the ones it would deny, but
forwards everything. `enforce` answers those with 403 and Docker's error format, so the
Docker SDK raises a normal `APIError` with the reason.

### Policy (deny unless listed)

"Managed" below means a container labelled `flamekeeper.managed=true` (worlds),
`flamekeeper.managed.helper=true` (throwaway helpers) or `flamekeeper.role=self-update`.

| Request | Rule |
|---|---|
| `/_ping`, `/version`, `/info`, `GET /containers/json`, `GET /containers/{id}/json`, `GET /images/…/json`, `GET /distribution/…/json`, `GET /networks…`, `GET /volumes…`, `GET /exec/{id}/json` | Allowed (reads). |
| `POST /images/create` | Only allowlisted images: the game repo (any tag) and the helper images. |
| start, stop, restart, kill, wait, `DELETE`, logs, stats, archive `GET`/`HEAD` on a container | Managed containers. Unmanaged ones only when the current **import grant** names them (the importer stops, starts, reads and removes the old server's container). |
| `POST /containers/{id}/update` | As above, and the body may only set `RestartPolicy`. |
| `POST /containers/{id}/exec` | World containers only; not privileged, no user override. |
| `POST /exec/{id}/start`, `/exec/{id}/resize` | Allowed (the exec was checked when it was created). |
| `POST /networks/create` | Only the `flamekeeper` network, bridge driver. |
| `POST /networks/{id}/connect` | Only that network, only for world containers or the controller (`flamekeeper.role=controller`). |
| `POST /volumes/create` | Only the data volume (`flamekeeper-data`): local, `type=none,o=bind,device=DATA_DIR`. |
| `DELETE /volumes/{name}` | Only while an import grant is in effect (importer cleanup); never the data volume. |
| `POST /containers/create` | One of the two shapes below. |
| Anything else (build, plugins, swarm, secrets, attach, archive `PUT`, commit, …) | Denied. |

**World container** (`flamekeeper.managed=true`, `flamekeeper.world_id` set):
image from an allowlisted repo; in `HostConfig` only `Mounts`, `PortBindings`,
`RestartPolicy`, `Devices`, `NanoCpus`, `Memory`, `LogConfig` and `NetworkMode` may be
non-empty (no `Binds`); exactly one mount: the data volume with subpath
`worlds/<slug>/game`, requested with API 1.45 or newer, while the volume is bound to
`DATA_DIR`; `NetworkMode` is the `flamekeeper` network (or `default`/`bridge`); devices
only `/dev/ntsync`; ports UDP ≥ 1024.

**Helper** (`flamekeeper.managed.helper=true`): `busybox:stable` or the steamcmd image;
`HostConfig` may only set `Binds`, `Mounts`, `Devices`, `NetworkMode`, `VolumesFrom`,
`LogConfig`. `Binds` only of `DATA_DIR` itself. `Mounts` only read-only binds (Docker
refuses a missing source instead of creating it) of `DATA_DIR` or exactly a folder the
import grant names, which may not be in or above `DATA_DIR`. `VolumesFrom` only `:ro` and only
for a container the import grant names. `Devices` only `/dev/ntsync`. `network_mode: host`
only for the exact command `cat /proc/net/udp /proc/net/udp6` with no mounts.

Everything else, including the current privileged self-update helper, is denied.

### World folders: volume subpaths, never binds below DATA_DIR

FlameKeeper can write anything in `DATA_DIR`, so it can replace any folder **in** it with a
symlink. Docker resolves a bind source when it *starts* a container, not when it creates
it, and Docker starts containers on its own (restart policy, crashes, reboots) without
asking the proxy. So a check at create time can't protect a bind of
`DATA_DIR/worlds/<slug>/game`: FlameKeeper could swap `game` for a symlink to `/`
afterwards, and the next start would mount the host's root (tested: Docker follows a
symlink swapped in after create). Checking each path for symlinks only moves the problem.

What FlameKeeper can't swap is `DATA_DIR` itself: it's the root of its mount, and it has
no access to the parent. So nothing below `DATA_DIR` is ever a mount source:

- **World containers** mount their game folder from the `flamekeeper-data` volume (a
  local volume bound to `DATA_DIR`, created on first use) with the subpath
  `worlds/<slug>/game`. Docker 26 (API 1.45) resolves a subpath without leaving the
  volume and pins what it found. Tested on Docker 29: a final folder, a parent folder,
  absolute and relative symlinks, to `/` or to folders that exist, all fail to start
  with "path concatenation escapes the base directory", including starts Docker does by
  itself; restoring the folder makes the container start again.
- **Helpers** may bind `DATA_DIR` itself (they work inside it, at `/data`), never a
  folder in it; read-only mounts are `DATA_DIR` or exactly a granted host folder outside
  it.
- The proxy only allows creating the data volume with exactly those options, refuses to
  remove it, and checks that it really is bound to `DATA_DIR` before a world container
  is created (Docker would otherwise create an empty volume by that name).

This also means the proxy doesn't look at the filesystem at all: it has no `DATA_DIR`
mount and no capabilities.

### Import grants

The importer legitimately touches a container FlameKeeper didn't create, or reads a host
folder outside `DATA_DIR`. The proxy allows that only for what the host admin names in
`$FK_PROXY_STATE_DIR/import.json`:

```json
{"until": 1795000000, "paths": ["/srv/enshrouded"], "containers": ["enshrouded"]}
```

That directory is mounted only into the proxy, so FlameKeeper can't grant itself
anything. A plain "import window" that allowed any read-only host path isn't safe: a
read-only mount still lets a process **connect to Unix sockets** in it, so mounting `/run`
read-only would reach `docker.sock`. Hence:

- `paths` must match the mount source **exactly**. The proxy can't see the host outside
  `DATA_DIR`, and Docker follows a symlink in a mount source, so a subfolder could be a
  symlink to `/run`. Symlinks *inside* a mounted folder resolve inside the helper, which
  is harmless. The importer mounts the folder the user chose and works inside it.
- These are refused even when granted: `/run`, `/var/run`, `/var/lock`, `/proc`, `/sys`,
  `/dev`, `/var/lib/docker`, `/var/lib/containerd`, `/etc`, `/root`, `/boot`, anything in
  them, and anything above them (`/`, `/var`).
- `containers` match by name or ID; the grant covers stop/start/restart-policy/archive
  reads/removal of those containers, `volumes_from` them, and removing volumes.

The host command is `sudo flamekeeper import --allow PATH|CONTAINER...` (one hour; running
it again replaces the grant; `--done` withdraws it, no argument shows it). It resolves
folders through symlinks (and says so when that changes the path the user should enter),
refuses `/`, paths with quotes, backslashes or control characters (the file is JSON
written by a shell script), unknown containers and FlameKeeper's own. The proxy answers
`GET /_fk/import-grant` itself with the current grant, so the importer checks before
scanning, creating and each cleanup step, and the UI shows the exact command to run
instead of an import failing halfway (some archive reads would otherwise look like "no
savegame found").

## Code changes the policy needs

1. **Importer** (done in step 2): mounts only the folder the user chose (read-only
   `Mounts`, so a typo can't create folders on the host) and looks for the game install
   inside it; `volumes_from` is `:ro`.
2. **ntsync check** (done in step 2): a helper started with `devices=["/dev/ntsync"]`;
   Docker refuses to start it when the host lacks the device. `run_helper` now removes its
   container even when the start fails.
3. **Self-update** (done in step 3): the controller writes `VERSION REQUEST_ID` to
   `DATA_DIR/self-update/request`. The installer's `flamekeeper-update.path` unit starts
   `flamekeeper-update.service`, which runs `flamekeeper update-request` as root. That
   treats the file as hostile (FlameKeeper can write anything in `DATA_DIR`): no symlinks,
   FIFOs or symlinked folder, at most 100 bytes, `^\d+\.\d+\.\d+$` and a 16-hex ID,
   a version newer than the installed one, and the download only exists for a real
   release tag. The log and `status.json` go to `/opt/flamekeeper/self-update`, mounted
   **read-only** into the controller: if root wrote them into `DATA_DIR`, the controller
   could replace `update.log` with a symlink to `/etc/passwd`. The controller fails the
   run if nothing picks up the request within 90 seconds, and removes it so it can't
   fire later. Without systemd the installer leaves no `updater` marker and the UI says
   to run `sudo flamekeeper update`. `run_detached`, `privileged` and `pid_host` are gone;
   the proxy still lets FlameKeeper remove an old `flamekeeper.role=self-update`
   container, which a controller updated by the old helper does once.

## Compose (step 4)

See [`installer/compose.template.yml`](../installer/compose.template.yml) (and
[`docker-compose.example.yml`](../docker-compose.example.yml), the same shape for people
who manage compose themselves). In short:

- `dockerproxy` (container `flamekeeper-dockerproxy`): the host's Docker socket, the
  shared `docker-proxy` volume at `/run/fk`, and `/opt/flamekeeper/proxy` (the import
  grant) read-only; no network, read-only root, no capabilities, `no-new-privileges`.
  Healthy once its socket exists.
- `flamekeeper`: no Docker socket; `DOCKER_HOST=unix:///run/fk/docker.sock`, the label
  `flamekeeper.role=controller` (so it may join the `flamekeeper` network), and it starts
  only after the proxy is healthy.
- `FK_PROXY_MODE` comes from `.env` (default `enforce`) and is kept across re-installs;
  `audit` is the escape hatch if enforcement ever blocks something legitimate.

Existing installs move over on their next update: the old self-update runs the new
release's installer, which writes this compose file; `compose up` creates the proxy, waits
for it, and recreates the controller. Game containers keep running throughout.

## Steps

1. **Proxy in audit mode** — done. `dockerproxy/` (policy + unit tests built from the Docker
   SDK's real request bodies), built into the image, CI job, and
   `scripts/dockerproxy-e2e.sh` to run a controller through it. An e2e run on Docker 29.6
   (startup, self-check, world create/start/process restart via exec/stop/delete, logs,
   stats, steamcmd update check, import scans) only logged the expected would-denies: the
   `/dev` bind (ntsync check), `volumes_from` without `:ro` and archive reads of the old
   server's container (import scan), and the read-only `/` bind (host-path import). The
   self-update helper wasn't exercised (needs a release) and is covered by unit tests.
2. **ntsync check and importer** — done (code changes 1–2 above; import grants replace
   the import window). An e2e run in **enforce** mode: startup, self-check, the world
   lifecycle, and both a host-folder import (with game files) and a container import
   (with the old container's and volume's cleanup) under a grant, with nothing denied;
   without a grant both imports are refused. The run found that the proxy's
   unprivileged user couldn't look inside FlameKeeper's `0700` import folders, so the
   proxy ran as root with only `CAP_DAC_READ_SEARCH` (step 5 removed the need: it now
   has no capabilities and no `DATA_DIR` mount).
3. **Host-side self-update** — done (code change 3 above). Unit and installer tests cover
   hostile requests (injection-shaped versions, symlinks, FIFOs, a symlinked folder,
   downgrades). `e2e-clean-host.sh` checks the unit on real systemd and that a malformed
   request is refused without running anything; `e2e-self-update.sh` updated a 0.0.1
   install to the 0.6.3 release through the UI's API via the unit, with no helper
   container. Writing it also found that `flamekeeper update` reported success when the
   installer failed, if its caller tested its status (`set -e` doesn't apply there).
4. **Enforce mode for installs** — done. The installer's compose and the example run the
   proxy in enforce mode and FlameKeeper without the socket; `flamekeeper import`
   writes grants; the importer checks the grant first and shows the command. Tested:
   `e2e-clean-host.sh` (on a real install, the controller has no socket, a privileged
   container from inside it is refused, and a host-folder import is refused with the
   command, then allowed), `e2e-self-update.sh` (UI update from behind the proxy), and an
   upgrade of a real 0.6.3 install with a world to this build (proxy created and healthy
   before the controller, the world still managed, its container recreated through the
   proxy, nothing denied). Writing it also found that busybox `realpath` has no `-e`.
5. **World folders as volume subpaths** — done. Planned as "run FlameKeeper as non-root to
   close the race between the symlink check and Docker's mount", but testing showed it
   wasn't a race: Docker resolves bind sources at every start, including its own
   restarts, so a swapped folder was a deterministic way out for a compromised
   FlameKeeper, and non-root wouldn't have helped (helpers run as root in `DATA_DIR`).
   Fixed instead as described in "World folders" above; FlameKeeper now needs Docker
   Engine 26 (installer and self-check). Existing world containers show "restart
   required" until recreated (the infra fingerprint includes the mount). Tested on real
   Docker through the enforcing proxy: the world lifecycle, both imports, the swap
   attack (refused at FlameKeeper's start and at Docker's own), the old bind shape, a
   helper bind inside `DATA_DIR` and a volume bound to `/` (all refused), and an
   upgrade of a 0.6.3 install whose world showed "restart required" and switched to the
   subpath when recreated.

Once enforcing, a compromised FlameKeeper can only run unprivileged world and helper
containers working on `DATA_DIR`; it can wreck the game servers, not take over the host.
