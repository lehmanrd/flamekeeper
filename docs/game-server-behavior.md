# How the game server behaves

What FlameKeeper relies on about the Enshrouded dedicated server and the
[`mornedhels/enshrouded-server`](https://github.com/mornedhels/enshrouded-server) image.
Everything here was checked against a real server (game build 23178631, settings readme
0.9.0.0). The code links here where it depends on one of these facts.

## The config file

FlameKeeper treats `enshrouded_server.json` as the source of truth for game settings and
roles. The image leaves `gameSettings` and `userGroups` alone when no
`SERVER_GS_*`/`SERVER_ROLE_*` environment variables are set, through both process and
container restarts, including roles that were deleted.

**Server fields are not passed as environment variables.** The image's bootstrap rewrites
the file from `SERVER_NAME`, `SERVER_SLOT_COUNT`, `SERVER_IP`, `SERVER_VOICE_CHAT_MODE`,
`SERVER_ENABLE_VOICE_CHAT` and `SERVER_ENABLE_TEXT_CHAT` on **every container start**.
Environment variables are fixed when the container is created, so a later edit in
FlameKeeper would be reverted on the next container restart. Each field is guarded by
`if [[ -n "$VAR" ]]` in the image, so leaving them unset leaves the file alone. The only
server variable FlameKeeper sets is `SERVER_QUERYPORT`, which the image uses for its own
"players online" check; changing the port already recreates the container.

**The server rewrites the file on every boot.** It re-serializes the whole file in its own
format (tabs, its own number formatting such as `2` → `2.0`), fills in missing
`gameSettings`, and **drops keys it doesn't know**, both top-level and inside
`gameSettings`. FlameKeeper's writes keep every key it doesn't manage (tags, bans,
directories), but the file can't hold extra data of our own.

**The server also rewrites the file while running**, when a player is banned or unbanned
in game. That write comes from the server's in-memory config, so it would undo any change
written to the file since the server started. FlameKeeper therefore never writes the live
file while the server runs: saves are staged in `config-history/`, and applying them is
stop → merge the managed keys into the live file (keeping bans and anything else the server
wrote) → start. A stopped world gets saves applied straight away.

`supervisorctl restart enshrouded-server` re-reads the file; the server is back online about
10 seconds later. A container restart takes about 12 seconds.

### Values the server accepts

- **Enums are strict.** An invalid value makes the server print `Supported '<Enum>'
  values:` with the full list, then exit with `Internal Error: Failed to parse Config`, and
  supervisord retries in a loop. The accepted lists match `settings-schema.json` exactly;
  `backend/tests/fixtures/server-enums.json` holds them and a test keeps the two in step.
- **`tags` is an enum list** (`DedicatedServerTagFlag`), not free text. An unknown tag
  stops the server from booting. Accepted: English, German, French, Italian, Japanese,
  Korean, Polish, Portuguese, Russian, Spanish, Thai, Turkish, Ukrainian, Chinese,
  Taiwanese, LookingForPlayers, BaseBuilding, Exploration, Roleplay.
- Game version 1.0.0.0 added `flameLevelFogRemoval` (the Flame level needed before Flame
  Altars can be placed in the Shroud; whole numbers 0–20, default 11) and a new
  `randomSpawnerAmount` value, `Automatic`. The server writes `flameLevelFogRemoval: 11` into
  existing configs on its first 1.0 boot. Both come from Keen's 1.0.0.0 readme: the
  `Automatic` label in game and per-preset values for the new setting aren't checked yet.
- `curseModifier` accepts only `Easy` (shown in game as "Off"), `Normal` and `Hard`.
- `randomSpawnerAmount` `"Extreme"` is shown in game as "Many", and `"Many"` as "More".
  `aggroPoolAmount` `"Few"` is shown in game as "Easy". FlameKeeper's dropdowns use the
  readme's option names instead, so they match the values in `enshrouded_server.json`.
- Night length goes up to 60 minutes; `playerBodyHeatFactor` is a continuous 0.5–2 range.
- The legacy top-level `password` is never written by the image or the server. An empty
  `"password": ""` creates no role, and the server drops the key on its next rewrite.
- The settings the server echoes at boot are in an internal format (`enemyDamageFactor` 2
  prints as `"40000000"`, the bits of 2.0f in hex), so they can't be used to check values.

### Default roles

If the image creates the file (none exists), it writes `"userGroups": []`, and the server
then adds a single `Default` role with a random password, not the four documented roles.
FlameKeeper writes a complete file with the four documented roles before a world's first
start, so this never happens to a world it creates.

## Bans

The server writes bans as:

```json
"bannedAccounts": [
	{"accountId": 12345678901234567891, "displayName": "Ashwalker",
	 "characterName": "Emberling", "banDate": {"value": 1792031497}}
]
```

- Keen's readme calls the list `bans` with `{accountIDHash, displayName, characterName,
  banDate}`. FlameKeeper keeps both keys.
- `accountId` is an unsigned 64-bit number, larger than JavaScript can represent exactly.
  The backend keeps it exact; any UI that shows or edits bans must treat it as text.
- A kicked or banned player gets **no** `Remove Player 'name'` line, only
  `[online] Disconnecting peer` and `[session] Remove player from session` without a name.
  FlameKeeper closes such sessions once A2S reports 0 players twice in a row.
- Unbanning logs `[server] Account <name>/<character> was unbanned.`; banning logs no named
  line.

## Paths

The image installs the server to `/opt/enshrouded/server`, so in a world's `game/` folder:

| What | Path |
|---|---|
| Config | `server/enshrouded_server.json` |
| Saves | `server/savegame/` |
| Game log | `server/logs/enshrouded_server.log` |
| Steam app manifest | `server/steamapps/appmanifest_2278520.acf` |
| Installed build | `current_version` (written by the image's updater; not on the very first install) |
| Image backups | `server/backups/` |
| Proton prefix | `server/steamapps/compatdata/2278520/pfx` |

The installed build comes from `current_version` when it exists, otherwise from the app
manifest. The first install downloads about 8.8 GB and takes about 3 minutes to reach
`HostOnline` on a typical host.

## Servers from other images

Importing (`backend/app/services/import_service.py`) finds a server by its files, since
images differ in where they keep them. The sknnr row is from that image's documentation and
hasn't been checked against a real container; the importer doesn't depend on either layout:

| Image | Install / config | Savegame |
|---|---|---|
| `mornedhels/enshrouded-server` | `/opt/enshrouded/server/enshrouded_server.json` | `/opt/enshrouded/server/savegame` (the whole `/opt/enshrouded` is usually one mount) |
| `sknnr/enshrouded-dedicated-server` | `/home/steam/enshrouded/enshrouded_server.json`, written from env vars on start, **not** in a volume | `/home/steam/enshrouded/savegame` (the volume) |

So the importer runs `find` over the container's mounts in a busybox helper started with
`--volumes-from`, which works on stopped containers too. That finds the save folder, and the
config when it's in a mount. Otherwise it reads `enshrouded_server.json` next to the save
folder, or in a few known install folders, with `docker cp` (`get_archive`), which also
reaches the container's own files. Images that build the config from environment variables
have written them into the file on their last start, so the file is the full picture. The
save folder comes from the config's `saveDirectory`, relative to the config's folder (the
server's working directory).

The game files are the folder holding `enshrouded_server.exe`, usually next to the
config. They're copied before the old server is stopped: they only change when it updates.
From a container they're streamed through Docker's archive API (`docker cp`), which also
reaches files outside the mounts. On this host 13 GB took about a minute. Symlinks are kept
as links, as a Proton prefix needs, but nothing is written through one. `current_version`
isn't copied, so the image's updater runs steamcmd on the first start, which checks the
files and downloads only what differs.

A folder on the host is read through a helper with the host's `/` mounted read-only at
`/host`, not by mounting the folder itself: Docker creates a missing bind-mount source as
an empty folder on the host.

## Game updates

On every container start the image runs `enshrouded-updater`. It compares
`current_version` with `depots.branches.<branch>.buildid` from
`https://api.steamcmd.net/v1/info/2278520`, and if they differ and nobody is online, it
stops the server, runs `steamcmd app_update 2278520 <branch> validate`, writes
`current_version` and starts the server. `supervisorctl start enshrouded-updater` does the
same on a running container. `enshrouded-force-update` deletes `current_version` and
`steamapps/` (manifests included) first.

steamcmd sometimes fails once and works on the next try: on 2026-10-16 an update logged
`Error! App '2278520' state is 0x6 after update job`, and the updater's second run
installed it 24 seconds later. The updater runs steamcmd at most twice. After the first
failure it logs `Download of Enshrouded server failed - retry`. After a second failure it
logs `… - aborting update` and starts the **old** version of the server. FlameKeeper's
update job therefore treats steamcmd's `state is 0x…` errors as fatal only after the
updater gives up. The world card keeps showing a given-up update even after the old
version comes online.

FlameKeeper's update job: wait until empty (optional) → `supervisorctl stop
enshrouded-server` (the server saves) → backup → merge pending config → `supervisorctl
start enshrouded-updater` → follow steamcmd progress → wait for `HostOnline` → check the
installed build. A stopped world is updated by starting it and stopping it again.

To find the latest build, FlameKeeper runs `steamcmd +login anonymous +app_info_update 1
+app_info_print 2278520 +quit` in the `steamcmd/steamcmd` image (about 17 seconds; sample
output in `backend/tests/fixtures/steamcmd-app-info-2278520.txt`), with api.steamcmd.net as
the fallback.

The image defaults `STEAMCMD_ARGS` to `"$GAME_BRANCH validate"`; FlameKeeper sets it only
when the user changes it.

Other supervisord programs in the image: `enshrouded-backup`, `enshrouded-restart`,
`enshrouded-reset-roles` and `enshrouded-bootstrap`.

## Proton and Wine builds

The image comes in a Proton build (`latest`, `stable-proton`) and a Wine build
(`stable-wine`). With game version 1.0, the Proton build fails at start and the server
crash-loops; the Wine build runs it. FlameKeeper therefore creates new worlds with
`stable-wine`. Switching an existing world means changing its image tag on the Server tab;
the game files and savegame stay as they are.

### Proton prefix

The Proton prefix contains directory symlinks (e.g. `dosdevices/c:` → `../drive_c`).
Copying a world's game files without them makes the server fail at once with
`wine: could not load kernel32.dll, status c0000135`, and supervisor gives up after a few
retries (`gave up: enshrouded-server entered FATAL state`). FlameKeeper copies with symlinks
preserved and reports both lines as failures with a hint. A force update rebuilds the
prefix.

## Savegames

**Game 1.0 changed the format.** The save is `savegame/world0.ksd`, with rolling copies in
`savegame/backup/world0.ksd.backup-N`. On a world's first 1.0 boot the server converts the
old save and moves the old files into `savegame/migrated/`. FlameKeeper backs up and
restores the whole folder, including `backup/` and `migrated/`; when it looks for the save
folder in a zip, `world0.ksd` (or old files outside `migrated/` and `backup/`) marks it.
FlameKeeper 0.4.0 and older picked `migrated/` instead, so restoring a backup made after
the update would have brought back the pre-1.0 world.

Before 1.0, `savegame/` held one world slot, `3ad85aea` (8 hex characters), plus rolling older copies
`3ad85aea-1` … `3ad85aea-9` and `3ad85aea-index`, which points at the latest. The server
saves about every 5 minutes and writes a new copy about every 10, even with nobody online.
A fresh world is about 20 KB. Characters are stored on the players' PCs, not on the server.

FlameKeeper backs up and restores the whole folder. The image's own backup program zips
only the latest slot plus a rewritten index, and only runs when `BACKUP_CRON` is set, which
FlameKeeper doesn't do; any such zips are listed as image backups.

## Keen's settings readme

Every server install ships `enshrouded_server_readme.txt` next to the executable. Version
0.9.0.0 documents every server field, all 37 `gameSettings` with defaults, ranges and enum
options, the 19 server tags, the role permissions and a full default config. The Windows
and Linux copies are identical.

Version 1.0.0.0 keeps the 0.9.0.0 text and adds a short entry at the top for each new
setting (`Default: "key": value`, `Options: 0-20`); the parser reads both forms.
`app/services/server_readme.py` parses it and compares it with `settings-schema.json`; for
1.0.0.0 everything matches. FlameKeeper re-reads the readme whenever a world comes online
(a game update may have replaced it). If it changed and lists anything the schema doesn't
cover, the world's event log gets a warning, and Settings → Game settings readme shows the
differences. To update after a game patch, fix `settings-schema.json` until the readme
check in Settings shows no differences, and update the test excerpt in
`backend/tests/fixtures/` if the format changed.

Known mistakes in the readme: the `perkUpgradeRecyclingFactor` example says 0.1, but the
real default (and the default file's value) is 0.5; the `aggroPoolAmount` description
mentions a percentage, but the setting is an enum.

## Log lines

`docker logs` carries everything through supervisord and rsyslog. Game lines look like:

```
2026-10-14 19:55:47.419 supervisord: enshrouded-server [Session] 'HostOnline' (up)!
```

Tabs inside messages show as `#011`, and steamcmd's color resets as `#033[0m` (the log
viewer strips them). Supervisord's own lines use a comma before the milliseconds:
`2026-10-14 19:55:37,110 INFO spawned: 'enshrouded-server' with pid 271`.

| Event | Sample |
|---|---|
| Process start | `enshrouded-server INFO - Running enshrouded-server` |
| Online | `enshrouded-server [Session] 'HostOnline' (up)!` |
| Going down | `enshrouded-server [Session] 'HostOnline' (down)!` |
| Shutdown begins | `[app] Trigger gameflow shutdown, exit: Ctrl_C` |
| Save | `[server] Start Saving` … `[server] Saved` |
| Shutdown done | `enshrouded-server INFO - Shutdown complete` |
| steamcmd progress | `enshrouded-updater  Update state (0x61) downloading, progress: 61.89 (5453324751 / 8811579458)` |
| Invalid config | `Failed to parse enum 'CurseModifier: Invalid value 'NotARealValue'.` then `Internal Error: Failed to parse Config` |
| Player join | `[server] Player 'Wanderer' logged in with Permissions:`, then one `#011 - CanKickBan`-style line per permission |
| Player leave | `[server] Remove Entity for Player 'X'` then `[server] Remove Player 'X'`; FlameKeeper matches only the second |
| Connection (no name) | `[online] Client '<steamid64>' authenticated by steam`; a failed connect shows `Disconnecting peer` without a login line |

The game's own `logs/enshrouded_server.log` has the same game lines but none of the
supervisord or steamcmd lines, so the container log is enough.
