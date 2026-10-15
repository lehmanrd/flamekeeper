# FlameKeeper

A self-hosted web app to create, run and monitor **Enshrouded dedicated servers** on
Linux. Each world runs in its own [`mornedhels/enshrouded-server`](https://github.com/mornedhels/enshrouded-server)
container; FlameKeeper creates and manages those containers for you.

- **Worlds:** a new-world wizard, several worlds side by side, start/stop/restart, live
  status, player counts and logs.
- **Settings:** difficulty presets or custom values for every option, roles and passwords,
  server name, chat and tags, with history and import/export.
- **Backups:** scheduled and manual, download and upload, restore, and new worlds cloned
  from a backup.
- **Updates:** game and container image updates, automatic or manual, with a backup first.
- **Monitoring:** CPU, memory, network and player charts, and player history.
- **Access:** an admin login, and Discord notifications for server events.

FlameKeeper is an independent project, not affiliated with Keen Games or the
mornedhels image.

## Install

On a Linux x86-64 machine (Ubuntu, Debian, Fedora, RHEL/Rocky/Alma, Arch, openSUSE):

```sh
curl -fsSL https://github.com/lehmanrd/flamekeeper/releases/latest/download/install.sh | bash
```

Then open `http://<your-server>:8080`.

What the installer does, in order:

1. Checks the machine: Linux, x86-64 (the Enshrouded server is x86-64 only), systemd.
2. Installs Docker if it's missing (it asks first), using Docker's official script or
   your distro's packages, and the `docker compose` plugin.
3. Warns (but doesn't stop) if the machine is below Keen's guidance per server:
   16 GB RAM, 6 CPU cores, 30 GB disk.
4. Writes `/opt/flamekeeper/docker-compose.yml` and `.env`, creates `/srv/flamekeeper`.
5. Offers to open the UI port and game ports (UDP 15637–15646) in ufw or firewalld.
6. Starts FlameKeeper and waits until its self-check passes.
7. Asks for the admin username and password, then prints the URL.

It never runs anything until fully downloaded. Re-running it upgrades in place and keeps
your settings. Options go after `bash -s --`:

```sh
curl -fsSL …/install.sh | bash -s -- --yes --port 8090 --data-dir /mnt/games --bind 127.0.0.1
```

| Option | Meaning |
|---|---|
| `--yes` | Don't ask questions (needed without a terminal) |
| `--version 0.2.0` | Install a specific release |
| `--data-dir PATH` | Where worlds, saves and backups live (default `/srv/flamekeeper`) |
| `--port N` / `--bind IP` | Web UI port and address (default `0.0.0.0:8080`) |
| `--no-docker-install` | Fail instead of installing Docker |
| `--no-firewall` | Leave ufw/firewalld alone |
| `--admin-user NAME` | Admin username (default `admin`); with `FLAMEKEEPER_ADMIN_PASSWORD` set, no password prompt |

Prefer to read it first? Download `install.sh` and `install.sh.sha256` from the
[latest release](https://github.com/lehmanrd/flamekeeper/releases/latest), run
`sha256sum -c install.sh.sha256`, read it, then `bash install.sh`.

### The `flamekeeper` command

```
flamekeeper status            controller and game servers
flamekeeper logs [-f]         controller logs
flamekeeper update            upgrade to the latest release (keeps settings)
                              [--version X] [--no-firewall]
flamekeeper doctor            check Docker, disk and the self-check
flamekeeper reset-admin       set the admin username and password (e.g. forgotten password)
flamekeeper start|stop|restart
flamekeeper uninstall [--purge]
```

Stopping or uninstalling the controller **does not** stop your game servers. They keep
running and are picked up again when FlameKeeper starts. `uninstall --purge` removes
everything, including saves, after asking you to type `flamekeeper`.

### Without the installer

Use [`docker-compose.example.yml`](docker-compose.example.yml). The one rule: mount the data
directory at the **same path** inside and outside the container (`/srv/flamekeeper:/srv/flamekeeper`).
FlameKeeper creates game containers through the host's Docker, which needs host paths. It
checks this at startup and tells you if it's wrong.

### Updating FlameKeeper

FlameKeeper checks GitHub for a new release twice a day. When there is one, Settings in
the sidebar gets a **!** badge, and **Settings → FlameKeeper updates** has an "Update to …"
button. Updating takes about a minute: the web page is unavailable while FlameKeeper
restarts, then reloads by itself. Game servers keep running and players stay connected.
Settings, worlds and backups are kept. Discord gets a message when a release is out and
when the update finishes (or fails).

The button runs `flamekeeper update` on the server for you, so it's only offered on
installs made with the installer; otherwise the card shows what to run. To do it from a
terminal instead: `sudo flamekeeper update`. To stop the checks, turn off "Check GitHub
for new FlameKeeper releases" under Settings → Defaults & checks.

How it works: a container can't replace itself, so FlameKeeper starts a short-lived helper
container (privileged, in the host's process namespace) that runs the host's
`flamekeeper update --version X`. The output is kept in `<data dir>/self-update/update.log`.

## Signing in

The installer asks for the admin username and password, so FlameKeeper is never left
open for someone else to claim. For an unattended install, pass `--admin-user` and set
`FLAMEKEEPER_ADMIN_PASSWORD`. Sign-ins last 30 days; change the password under Settings.

Without the installer (or with `--yes` and no password set), the first visitor creates
the account in the browser, but only within 10 minutes of FlameKeeper starting. After that,
setup locks, so an install nobody finished doesn't stay open to whoever finds it. Restart
FlameKeeper to reopen it, or create the account from the terminal.

Forgot the password? `sudo flamekeeper reset-admin` sets a new username and password and
signs everyone out. Game servers keep running.

## Notifications

Settings → Discord notifications posts to a Discord channel through a webhook: servers
coming online, crashing or stopping, players joining or leaving, updates, restores, and
anything that fails. Pick which ones you want, and use "Send test" to check it works.

## Security

FlameKeeper serves plain HTTP, so on an untrusted network bind it to `127.0.0.1`
(`--bind 127.0.0.1`) and put it behind a reverse proxy with HTTPS (set `BASE_PATH` to
serve it under a sub-path). Anyone signed in can control your servers and read role
passwords. FlameKeeper also has access to the Docker socket, which is equivalent to root on
the host; updating from the UI uses that to run `flamekeeper update` on the host.

## Changing settings

Edits to difficulty, roles and server name/chat/tags are saved to a history (the last 50
are kept under `config-history/`, with a diff and one-click restore). For a
stopped world they go straight into its `enshrouded_server.json`. A running server owns
that file (it rewrites it when someone is banned in game), so changes wait in the history
and are merged into the file when FlameKeeper restarts the server: stop, merge,
start. Only the settings FlameKeeper manages are merged; the ban list stays as the server
wrote it. A "Restart required" banner shows while changes are waiting.
Container settings on the Server tab (port, image tag, PUID/PGID, limits) apply when the
container is recreated, or automatically on the next start of a stopped world.

New worlds use the image's Wine build (`stable-wine`): since game version 1.0 the Proton
build (`latest`) crash-loops on start. A world created with `latest` can be switched on its
Server tab; the save is kept. Change the default for new worlds under Settings.

## More than one world

Each world is its own container, data folder and UDP port (15637, 15638, … by default).
The new-world wizard suggests the next free port, checks it on the host, and can copy the
game files from an existing world instead of downloading about 9 GB again. Keen recommends
16 GB RAM, 6 CPU cores and 30 GB disk per server; FlameKeeper warns when your worlds
add up to more than the host has, but doesn't stop you (idle servers use far less).

## Backups

Each world is backed up hourly by default, keeping the last 48 scheduled backups and
skipping runs when nobody has played since the previous one. Change this on the world's
Backups page. Zips are in `<data dir>/worlds/<slug>/backups/` and contain the savegame and
`enshrouded_server.json` **including role passwords**. Backing up a running server is safe:
FlameKeeper copies the savegame between the server's saves (about every 10 minutes), or
you can choose to stop the server for the backup. A restore always saves the current state
as a "before restore" backup first.

## Updates

FlameKeeper asks Steam for the latest build of each world's branch every 30 minutes
(`UPDATE_CHECK_INTERVAL_MIN`), by running steamcmd in a short-lived container
(`steamcmd/steamcmd`, about 550 MB, pulled on first use). Set `STEAM_INFO_SOURCE=api` to
use api.steamcmd.net instead, or `STEAM_API_FALLBACK=true` to fall back to it. Each world
has a policy: manual, immediately, when nobody is online (the default), or in a
maintenance window. An update stops the server (it saves), backs it up, runs the image's
updater and waits until the server is back online. Container image updates are checked
daily and are notify-only by default.

## Players

Players join through the server browser or by IP and port (`<public-ip>:15637` for the
first world). Each world uses one UDP port; forward it on your router for players outside
your network. A player's role (Admin, Friend, Guest, Visitor) is decided by the password
they enter.

## Development

```
backend/    FastAPI controller (Python 3.12, uv)
frontend/   React + TypeScript + Vite UI
installer/  install.sh, the flamekeeper CLI, and their tests
docs/       game-server-behavior.md: how the game server and image behave, as FlameKeeper relies on it
```

```sh
# Backend: tests, then run against your local Docker (DATA_DIR must be writable)
cd backend && uv sync && uv run pytest
DATA_DIR=$HOME/fk-data uv run uvicorn app.main:get_app --factory --port 8080

# UI without Docker or a game server: fake Docker backend + Vite dev server
FAKE_DOCKER=true DATA_DIR=/tmp/fk uv run uvicorn app.main:get_app --factory --port 8080
cd frontend && npm install && npm run dev        # http://localhost:5173

# Frontend checks
npm run typecheck && npm test && npm run build

# Installer
docker run --rm -v "$PWD":/mnt koalaman/shellcheck:stable -x /mnt/installer/install.sh /mnt/installer/flamekeeper
docker run --rm -v "$PWD":/code -w /code bats/bats:latest installer/tests
installer/tests/e2e-clean-host.sh flamekeeper:dev   # fresh Ubuntu, installs Docker, then FlameKeeper
installer/tests/e2e-self-update.sh                  # same, then updates from the UI to the latest release (uses the internet)

# Image
docker build -t flamekeeper:dev .
```

The **Game server** workflow (`.github/workflows/game-server.yml`) installs a release on a
GitHub runner and runs a real Enshrouded server through it: download, online, settings
readme vs the schema, backup, restore, restart, stop. It runs after each release, every
Monday against the latest release, and on demand (Actions → Game server → Run workflow;
version `checkout` tests the branch instead of a release). The API part is
`scripts/ci/game_server_test.py`, which works against any FlameKeeper.

### Releasing

Push a tag `vX.Y.Z`. The release workflow builds `ghcr.io/<owner>/flamekeeper:X.Y.Z` (and
`:latest`), builds `install.sh` with that version pinned, and attaches it to a GitHub
release. After the first release, make the GHCR package **public** (package settings on
GitHub) so installs can pull it without logging in.

## License

[MIT](LICENSE).
