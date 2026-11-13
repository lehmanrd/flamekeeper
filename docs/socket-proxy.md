# Docker socket proxy

Status: **step 1 (audit mode) done** (see Steps). This is the plan for taking the Docker socket
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
- The proxy service: `network_mode: none`, read-only root filesystem, `cap_drop: [ALL]`,
  non-root user with the host's docker group added, and `DATA_DIR` mounted **read-only** at
  the same path so it can check bind paths on disk. That user needs search (`x`) permission
  on every directory down to `DATA_DIR` and its `worlds/<slug>` folders; the default
  `/srv/flamekeeper` has it. In enforce mode the proxy refuses to start if it can't see
  `DATA_DIR`, since every bind path would otherwise look nonexistent and pass the check.
- The image has `/run/fk` owned by the proxy user, so a fresh named volume mounted there is
  writable by it.
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
| start, stop, restart, kill, wait, `DELETE`, logs, stats, archive `GET`/`HEAD` on a container | Managed containers. Unmanaged ones only while the **import window** is open (the importer stops, starts, reads and removes the old server's container). |
| `POST /containers/{id}/update` | As above, and the body may only set `RestartPolicy`. |
| `POST /containers/{id}/exec` | World containers only; not privileged, no user override. |
| `POST /exec/{id}/start`, `/exec/{id}/resize` | Allowed (the exec was checked when it was created). |
| `POST /networks/create` | Only the `flamekeeper` network, bridge driver. |
| `POST /networks/{id}/connect` | Only that network, only for world containers or the controller (`flamekeeper.role=controller`). |
| `DELETE /volumes/{name}` | Only while the import window is open (importer cleanup). |
| `POST /containers/create` | One of the two shapes below. |
| Anything else (build, plugins, swarm, secrets, attach, archive `PUT`, commit, …) | Denied. |

**World container** (`flamekeeper.managed=true`, `flamekeeper.world_id` set):
image from an allowlisted repo; in `HostConfig` only `Binds`, `PortBindings`, `RestartPolicy`,
`Devices`, `NanoCpus`, `Memory`, `LogConfig` and `NetworkMode` may be non-empty;
`NetworkMode` is the `flamekeeper` network (or `default`/`bridge`); devices only
`/dev/ntsync`; ports UDP ≥ 1024; binds only from under `DATA_DIR/worlds/`.

**Helper** (`flamekeeper.managed.helper=true`): `busybox:stable` or the steamcmd image;
`HostConfig` may only set `Binds`, `NetworkMode`, `VolumesFrom`, `LogConfig`; binds from
under `DATA_DIR`, or read-only from elsewhere while the import window is open;
`VolumesFrom` only `:ro` and only while the import window is open; `network_mode: host`
only for the exact command `cat /proc/net/udp /proc/net/udp6` with no binds.

Everything else, including the current privileged self-update helper, is denied.

### Bind paths and symlinks

Docker follows symlinks in a bind source, and FlameKeeper can write to `DATA_DIR`, so
`DATA_DIR/worlds/x/game -> /` would pass a prefix check. The proxy requires a clean
absolute path (no `..`) and `lstat`s every existing component below `DATA_DIR` in its
read-only view, refusing any symlink. That leaves a race (swap a directory for a symlink
between the check and Docker's mount). Phase 2 closes it: FlameKeeper runs as a non-root
user and the per-world directories are root-owned, so it can write inside `game/` but
can't replace it.

### Import window

The importer legitimately touches containers FlameKeeper didn't create and reads host
paths outside `DATA_DIR`. Those requests are allowed only while
`$FK_PROXY_STATE_DIR/import-until` holds a Unix time in the future. That directory is
mounted only into the proxy (not into FlameKeeper), so only the host admin can open the
window: `flamekeeper import --enable` writes it with a one-hour expiry.

## Code changes the policy needs

1. **Importer**: replace the read-only bind of the host's `/` with a bind of the directory
   the user picks; add `:ro` to `volumes_from`.
2. **ntsync check**: start a helper with `devices=["/dev/ntsync"]` and treat a failed
   create as "not available", instead of binding `/dev`.
3. **Self-update**: the UI writes `DATA_DIR/self-update/request.json` with `{version}`; a
   systemd `.path` unit installed by the installer runs `flamekeeper update --version X`,
   which re-checks the version is a newer GitHub release. Without systemd the UI says to run
   `flamekeeper update` on the host. `run_detached`, `privileged` and `pid_host` go away.

## Compose (installer template, step 4)

```yaml
services:
  dockerproxy:
    image: ${FLAMEKEEPER_IMAGE}
    command: ["fk-dockerproxy"]
    restart: unless-stopped
    network_mode: none
    read_only: true
    cap_drop: [ALL]
    user: "65534:65534"
    group_add: ["${DOCKER_GID}"]
    volumes:
      - ${DOCKER_SOCK}:/var/run/docker.sock
      - fk-sock:/run/fk
      - ${DATA_DIR}:${DATA_DIR}:ro
      - ${INSTALL_DIR}/proxy:/var/lib/fk-dockerproxy:ro
    environment:
      - DATA_DIR=${DATA_DIR}
      - FK_PROXY_MODE=enforce
  flamekeeper:
    # ...as now, without the docker.sock mount
    labels: { flamekeeper.role: controller }
    depends_on: [dockerproxy]
    volumes:
      - fk-sock:/run/fk
      - ${DATA_DIR}:${DATA_DIR}
    environment:
      - DOCKER_HOST=unix:///run/fk/docker.sock
volumes:
  fk-sock:
```

Existing installs move over on their next update: the current self-update runs
`flamekeeper update`, which rewrites the compose file.

## Steps

1. **Proxy in audit mode** — done. `dockerproxy/` (policy + unit tests built from the Docker
   SDK's real request bodies), built into the image, CI job, and
   `scripts/dockerproxy-e2e.sh` to run a controller through it. An e2e run on Docker 29.6
   (startup, self-check, world create/start/process restart via exec/stop/delete, logs,
   stats, steamcmd update check, import scans) only logged the expected would-denies: the
   `/dev` bind (ntsync check), `volumes_from` without `:ro` and archive reads of the old
   server's container (import scan), and the read-only `/` bind (host-path import). The
   self-update helper wasn't exercised (needs a release) and is covered by unit tests.
2. ntsync check and importer changes.
3. Host-side self-update.
4. Enforce mode in the installer's compose; drop the socket from FlameKeeper.
5. Later: non-root FlameKeeper (closes the symlink race).

Once enforcing, a compromised FlameKeeper can only run unprivileged world and helper
containers working on `DATA_DIR`.
