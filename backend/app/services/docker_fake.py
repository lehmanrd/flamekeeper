"""In-memory DockerAdapter for tests and UI development (FAKE_DOCKER=true)."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import tarfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.docker_adapter import (
    ContainerInfo,
    ContainerSpec,
    ContainerStats,
    DockerError,
    ExecResult,
    Mount,
    ProgressCb,
)

HOST_ONLINE_LINE = (
    "2026-01-01 00:00:00.000 supervisord: enshrouded-server [Session] 'HostOnline' (up)!"
)


class FakeDockerAdapter:
    def __init__(self, *, boot_delay: float = 0.0, emit_host_online: bool = True) -> None:
        self.containers: dict[str, ContainerInfo] = {}
        self.specs: dict[str, ContainerSpec] = {}
        self.logs: dict[str, list[tuple[datetime, str]]] = {}
        self.images: set[str] = set()
        self.networks: set[str] = set()
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.calls: list[tuple[str, str]] = []
        self.host_udp_ports: set[int] = set()
        self.boot_delay = boot_delay
        self.emit_host_online = emit_host_online
        # Steam: the build a fresh install gets, and what steamcmd says is the latest.
        self.installed_build = "23178631"
        self.latest_build = "23178631"
        self.local_digests: dict[str, str] = {}
        self.registry_digests: dict[str, str] = {}
        self.update_delay = 0.0
        # Failed steamcmd runs before the next update succeeds (2 = the updater gives up).
        self.steam_errors = 0
        self._followers: dict[str, list[asyncio.Queue[str | None]]] = {}
        self.detached: list[tuple[str, str, list[str], bool, bool]] = []
        self._seq = 0
        # Containers FlameKeeper didn't create: files outside their mounts live in a
        # local folder per container path ("the container layer").
        self.layers: dict[str, dict[str, Path]] = {}
        self.volumes: set[str] = set()
        self.helper_calls: list[list[str]] = []

    # -- test helpers ----------------------------------------------------------------

    def _find(self, name_or_id: str) -> ContainerInfo | None:
        for c in self.containers.values():
            if c.name == name_or_id or c.id == name_or_id:
                return c
        return None

    def spec_of(self, name: str) -> ContainerSpec:
        """The spec the current container with this name was created from."""
        info = self._require(name)
        return self.specs[info.id]

    def _require(self, name_or_id: str) -> ContainerInfo:
        c = self._find(name_or_id)
        if not c:
            raise DockerError(f"No such container: {name_or_id}")
        return c

    def emit_log(self, name_or_id: str, line: str) -> None:
        c = self._require(name_or_id)
        entry = (datetime.now(UTC), line)
        self.logs.setdefault(c.id, []).append(entry)
        for q in self._followers.get(c.id, []):
            q.put_nowait(entry)

    async def _boot(self, c: ContainerInfo) -> None:
        if self.boot_delay:
            await asyncio.sleep(self.boot_delay)
        if self.emit_host_online and c.status == "running":
            self.emit_log(c.id, HOST_ONLINE_LINE)

    def _start(self, c: ContainerInfo) -> None:
        c.status = "running"
        c.started_at = datetime.now(UTC)
        c.exit_code = None
        self.emit_log(c.id, "supervisord: enshrouded-server INFO - Running enshrouded-server")
        try:
            asyncio.get_running_loop().create_task(self._boot(c))
        except RuntimeError:
            pass

    def _end_followers(self, c: ContainerInfo) -> None:
        # Like Docker: a follow stream ends when the container stops.
        for q in self._followers.get(c.id, []):
            q.put_nowait(None)

    def add_container(self, name: str, image: str, *, mounts: list[Mount] | None = None,
                      layer: dict[str, Path] | None = None,
                      ports: dict[str, list[int]] | None = None,
                      env: dict[str, str] | None = None, running: bool = True,
                      restart_policy: str = "unless-stopped",
                      auto_remove: bool = False) -> ContainerInfo:
        """A container started outside FlameKeeper. Mount sources are local folders."""
        cid = uuid.uuid4().hex
        info = ContainerInfo(id=cid, name=name, image=image, labels={},
                             status="running" if running else "exited", env=dict(env or {}),
                             mounts=list(mounts or []), ports=dict(ports or {}),
                             restart_policy=restart_policy, network_mode="bridge",
                             auto_remove=auto_remove,
                             started_at=datetime.now(UTC) if running else None)
        self.containers[cid] = info
        self.logs[cid] = []
        self.layers[cid] = dict(layer or {})
        self.volumes |= {m.name for m in info.mounts if m.type == "volume" and m.name}
        return info

    def _local(self, c: ContainerInfo, path: str) -> Path | None:
        """Where a path inside a container lives on this machine."""
        roots = [(m.destination, Path(m.source)) for m in c.mounts]
        roots += list(self.layers.get(c.id, {}).items())
        spec = self.specs.get(c.id)
        roots += [(t, Path(h)) for h, t in (spec.binds if spec else {}).items()]
        best = None
        for dest, local in roots:
            dest = dest.rstrip("/") or "/"
            if path == dest or path.startswith(dest.rstrip("/") + "/"):
                if best is None or len(dest) > len(best[0]):
                    best = (dest, local)
        if best is None:
            return None
        rel = path[len(best[0]):].lstrip("/")
        return best[1] / rel if rel else best[1]

    def crash(self, name_or_id: str, exit_code: int = 1) -> None:
        c = self._require(name_or_id)
        c.status = "exited"
        c.exit_code = exit_code
        c.finished_at = datetime.now(UTC)
        self._end_followers(c)

    # -- DockerAdapter -----------------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        return {"Version": "fake", "ApiVersion": "1.47", "Os": "linux", "Arch": "amd64"}

    async def get_container(self, name_or_id: str) -> ContainerInfo | None:
        return self._find(name_or_id)

    async def list_managed(self) -> list[ContainerInfo]:
        return [c for c in self.containers.values() if c.labels.get("flamekeeper.managed")]

    async def list_containers(self) -> list[ContainerInfo]:
        return list(self.containers.values())

    async def update_restart_policy(self, name_or_id: str, policy: str) -> None:
        c = self._require(name_or_id)
        self.calls.append(("update", f"{c.name}:{policy}"))
        c.restart_policy = policy

    async def get_archive(self, name_or_id: str, path: str, dest: Path) -> None:
        c = self._require(name_or_id)
        local = self._local(c, path)
        if local is None or not local.exists():
            raise DockerError(f"{path} doesn't exist in the container")
        with tarfile.open(dest, "w") as tar:
            tar.add(local, arcname=os.path.basename(path.rstrip("/")) or "root")

    async def path_exists(self, name_or_id: str, path: str) -> bool:
        local = self._local(self._require(name_or_id), path)
        return local is not None and (local.exists() or local.is_symlink())

    async def copy_tree_out(self, name_or_id: str, path: str, dest: Path, *,
                            skip_top: set[str], on_bytes) -> int:  # noqa: ANN001
        from app.services.backup_archive import extract_tree

        self.calls.append(("copy_out", self._require(name_or_id).name))
        tmp = dest.parent / f".fake-{uuid.uuid4().hex}.tar"
        try:
            await self.get_archive(name_or_id, path, tmp)
            with open(tmp, "rb") as f:
                return extract_tree(f, dest, skip_top=skip_top, on_bytes=on_bytes)
        finally:
            tmp.unlink(missing_ok=True)

    async def remove_volume(self, name: str) -> None:
        if await self.volume_users(name):
            raise DockerError(f"volume {name} is in use")
        self.calls.append(("remove_volume", name))
        self.volumes.discard(name)

    async def volume_users(self, name: str) -> list[str]:
        return [c.name for c in self.containers.values()
                if any(m.type == "volume" and m.name == name for m in c.mounts)]

    async def create_container(self, spec: ContainerSpec) -> ContainerInfo:
        if self._find(spec.name):
            raise DockerError(f"Conflict: container name {spec.name} in use")
        self.calls.append(("create", spec.name))
        cid = uuid.uuid4().hex
        info = ContainerInfo(
            id=cid,
            name=spec.name,
            image=spec.image,
            labels=dict(spec.labels),
            status="created",
            env=dict(spec.env),
        )
        self.containers[cid] = info
        self.specs[cid] = spec
        self.logs[cid] = []
        return info

    async def start(self, name_or_id: str) -> None:
        c = self._require(name_or_id)
        self.calls.append(("start", c.name))
        network = self.specs[c.id].network if c.id in self.specs else None
        if network and network not in self.networks:
            # Like Docker: a container can be created on a missing network, not started.
            raise DockerError(f"failed to set up container networking: network {network} "
                              "not found")
        if c.status != "running":
            self._write_savegame(c)
            server = self._server_dir(c)
            manifest = server / "steamapps" / "appmanifest_2278520.acf" if server else None
            if manifest and not manifest.exists():
                self._write_manifest(c, self.installed_build)
            if manifest and f'"{self.latest_build}"' not in manifest.read_text():
                # Like the image's bootstrap: the updater runs before the server starts.
                c.status = "running"
                c.started_at = datetime.now(UTC)
                c.exit_code = None
                asyncio.get_running_loop().create_task(self._update(c, force=False))
                return
            self._start(c)

    def _server_dir(self, c: ContainerInfo) -> Path | None:
        spec = self.specs.get(c.id)
        for host, target in (spec.binds if spec else {}).items():
            if target == "/opt/enshrouded":
                return Path(host) / "server"
        return None

    def _write_manifest(self, c: ContainerInfo, build: str) -> None:
        server = self._server_dir(c)
        if server is None:
            return
        try:
            (server / "steamapps").mkdir(parents=True, exist_ok=True)
            (server / "steamapps" / "appmanifest_2278520.acf").write_text(
                f'"AppState"\n{{\n\t"appid"\t\t"2278520"\n\t"buildid"\t\t"{build}"\n}}\n')
            # Like the image's updater (setCurrentVersion).
            (server.parent / "current_version").write_text(f"{build}\n")
        except OSError:
            pass

    async def _update(self, c: ContainerInfo, force: bool) -> None:
        """The image's enshrouded-updater: download if the build differs, then start."""
        self.emit_log(c.id, "supervisord: enshrouded-updater INFO - Running enshrouded-updater")
        server = self._server_dir(c)
        manifest = server / "steamapps" / "appmanifest_2278520.acf" if server else None
        current = manifest.read_text() if manifest and manifest.exists() else ""
        if force or f'"{self.latest_build}"' not in current:
            # Like the image's updater: steamcmd failures are retried once, then the
            # update is given up and the old version started.
            for attempt in range(2):
                if self.steam_errors <= attempt:
                    break
                self.emit_log(c.id, "supervisord: enshrouded-updater Error! App '2278520' "
                              "state is 0x6 after update job.")
                if attempt == 1:
                    self.emit_log(c.id, "supervisord: enshrouded-updater WARN - Download of "
                                  "Enshrouded server failed - aborting update")
                    self.emit_log(c.id, "supervisord: enshrouded-server INFO - Running "
                                  "enshrouded-server")
                    await self._boot(c)
                    return
                self.emit_log(c.id, "supervisord: enshrouded-updater WARN - Download of "
                              "Enshrouded server failed - retry")
            for pct in (12.5, 61.89, 100.0):
                if self.update_delay:
                    await asyncio.sleep(self.update_delay)
                done = int(8811579458 * pct / 100)
                self.emit_log(c.id, "supervisord: enshrouded-updater  Update state (0x61) "
                              f"downloading, progress: {pct:.2f} ({done} / 8811579458)")
            self.emit_log(c.id, "supervisord: enshrouded-updater Success! App '2278520' "
                          "fully installed.")
            self._write_manifest(c, self.latest_build)
        else:
            self.emit_log(c.id, "supervisord: enshrouded-updater INFO - Enshrouded server "
                          "is already the latest version")
        self.emit_log(c.id, "supervisord: enshrouded-server INFO - Running enshrouded-server")
        await self._boot(c)

    def _write_savegame(self, c: ContainerInfo) -> None:
        """Like the real server: create a savegame on first start (a world slot + index)."""
        spec = self.specs.get(c.id)
        if not spec:
            return
        for host, target in spec.binds.items():
            if target != "/opt/enshrouded":
                continue
            save = Path(host) / "server" / "savegame"
            if save.exists() and any(save.iterdir()):
                return
            try:
                save.mkdir(parents=True, exist_ok=True)
                (save / "3ad85aea").write_bytes(b"fake world " + c.name.encode())
                (save / "3ad85aea-index").write_text('{"latest": 0}')
            except OSError:
                pass

    async def stop(self, name_or_id: str, timeout: int) -> None:
        c = self._require(name_or_id)
        self.calls.append(("stop", c.name))
        c.status = "exited"
        c.exit_code = 0
        c.finished_at = datetime.now(UTC)
        self._end_followers(c)

    async def restart(self, name_or_id: str, timeout: int) -> None:
        c = self._require(name_or_id)
        self.calls.append(("restart", c.name))
        self._start(c)

    async def remove(self, name_or_id: str, force: bool = False) -> None:
        c = self._find(name_or_id)
        if not c:
            return
        if c.status == "running" and not force:
            raise DockerError("container is running")
        self.calls.append(("remove", c.name))
        self._end_followers(c)
        del self.containers[c.id]

    async def exec(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        c = self._require(name_or_id)
        if c.status != "running":
            raise DockerError(f"container {c.name} is not running")
        self.exec_calls.append((c.name, cmd))
        if cmd[:2] == ["supervisorctl", "start"] and cmd[2] in ("enshrouded-updater",
                                                                  "enshrouded-force-update"):
            asyncio.get_running_loop().create_task(
                self._update(c, force=cmd[2] == "enshrouded-force-update"))
            return ExecResult(0, f"{cmd[2]}: started\n")
        if cmd[:3] == ["supervisorctl", "stop", "enshrouded-server"]:
            self.emit_log(c.id, "supervisord: enshrouded-server [Session] 'HostOnline' (down)!")
            self.emit_log(c.id, "supervisord: enshrouded-server INFO - Shutdown complete")
            return ExecResult(0, "enshrouded-server: stopped\n")
        if cmd[:3] == ["supervisorctl", "start", "enshrouded-server"] or \
                cmd[:3] == ["supervisorctl", "restart", "enshrouded-server"]:
            self.emit_log(c.id, "supervisord: enshrouded-server INFO - Running enshrouded-server")
            asyncio.get_running_loop().create_task(self._boot(c))
            return ExecResult(0, "enshrouded-server: stopped\nenshrouded-server: started\n")
        return ExecResult(0, "")

    async def logs_tail(
        self, name_or_id: str, tail: int = 500, since: datetime | None = None
    ) -> list[str]:
        c = self._find(name_or_id)
        if not c:
            return []
        lines = [ln for ts, ln in self.logs.get(c.id, []) if since is None or ts >= since]
        return lines[-tail:] if tail else lines

    async def logs_follow(
        self,
        name_or_id: str,
        since: datetime | None = None,
        tail: int | None = 0,
        timestamps: bool = False,
    ) -> AsyncIterator[str]:
        c = self._require(name_or_id)
        q: asyncio.Queue[tuple[datetime, str] | None] = asyncio.Queue()
        history = [e for e in self.logs.get(c.id, []) if since is None or e[0] >= since]
        replay = history if tail is None else (history[-tail:] if tail else [])
        for entry in replay:
            q.put_nowait(entry)
        self._followers.setdefault(c.id, []).append(q)
        try:
            while True:
                item = await q.get()
                if item is None:
                    return
                ts, line = item
                if timestamps:
                    line = f"{ts.isoformat().replace('+00:00', 'Z')} {line}"
                yield line
        finally:
            self._followers[c.id].remove(q)

    async def image_exists(self, image: str) -> bool:
        return image in self.images

    async def pull(self, image: str, progress: ProgressCb | None = None) -> None:
        for pct in (0, 50, 100):
            if progress:
                progress(pct, f"Pulling {image}: {pct}%")
        self.images.add(image)

    async def ensure_network(self, name: str) -> None:
        self.networks.add(name)

    async def connect_network(self, network: str, name_or_id: str) -> None:
        self._require(name_or_id)

    async def run_detached(
        self,
        name: str,
        image: str,
        cmd: list[str],
        *,
        labels: dict[str, str],
        privileged: bool = False,
        pid_host: bool = False,
    ) -> ContainerInfo:
        """Starts nothing: tests finish the container with finish_detached()."""
        if self._find(name):
            raise DockerError(f"Conflict: container name {name} in use")
        self._seq += 1
        info = ContainerInfo(id=f"detached{self._seq:06d}", name=name, image=image,
                             labels=labels, status="running", started_at=datetime.now(UTC))
        self.containers[info.id] = info
        self.detached.append((name, image, cmd, privileged, pid_host))
        return info

    def finish_detached(self, name: str, exit_code: int) -> None:
        c = self._require(name)
        c.status, c.exit_code, c.finished_at = "exited", exit_code, datetime.now(UTC)

    async def run_helper(
        self,
        image: str,
        cmd: list[str],
        binds: dict[str, str] | None = None,
        network_mode: str | None = None,
        volumes_from: list[str] | None = None,
        timeout: int = 120,
    ) -> ExecResult:
        self.helper_calls.append(cmd)
        if cmd and cmd[0] == "find":
            return self._helper_find(cmd, binds or {}, volumes_from or [])
        if cmd[:2] == ["sh", "-c"] and "cp -a" in cmd[2]:
            return self._helper_game_copy(cmd, binds or {})
        if cmd[:2] == ["sh", "-c"] and "/out/" in cmd[2]:
            return self._helper_copy(cmd, binds or {})
        if cmd[:2] == ["test", "-f"] and binds and any(t.endswith(":ro") for t in binds.values()):
            local = self._helper_local(cmd[2], binds, [])
            return ExecResult(0 if local is not None and local.is_file() else 1, "")
        if cmd[0] == "cat" and binds:
            local = self._helper_local(cmd[1], binds, [])
            if local is None or not local.is_file():
                return ExecResult(1, f"cat: can't open '{cmd[1]}'\n")
            return ExecResult(0, local.read_text())
        joined = " ".join(cmd)
        if "+app_info_print" in joined:
            return ExecResult(0, FAKE_APP_INFO.replace("BUILD", self.latest_build))
        if "/proc/net/udp" in joined:
            lines = ["  sl  local_address rem_address   st"]
            for i, port in enumerate(sorted(self.host_udp_ports)):
                lines.append(f"  {i}: 00000000:{port:04X} 00000000:0000 07")
            return ExecResult(0, "\n".join(lines) + "\n")
        if binds and "test -f" in joined:
            # Path-match check: the marker exists if the host path exists locally.
            from pathlib import Path

            marker = joined.split("test -f ", 1)[1].split()[0]
            host_dir = next(iter(binds))
            container_dir = binds[host_dir]
            local = Path(host_dir) / Path(marker).relative_to(container_dir)
            return ExecResult(0 if local.exists() else 1, "")
        return ExecResult(0, "")

    def _helper_local(self, path: str, binds: dict[str, str],
                      volumes_from: list[str]) -> Path | None:
        for host, target in binds.items():
            target = target.removesuffix(":ro")
            if path == target or path.startswith(target + "/"):
                return Path(host) / path[len(target):].lstrip("/")
        for name in volumes_from:
            c = self._require(name)
            # --volumes-from shares mounts only, not the container layer.
            for m in c.mounts:
                if path == m.destination or path.startswith(m.destination + "/"):
                    return Path(m.source) / path[len(m.destination):].lstrip("/")
        return None

    def _helper_find(self, cmd: list[str], binds: dict[str, str],
                     volumes_from: list[str]) -> ExecResult:
        roots = []
        for arg in cmd[1:]:
            if arg.startswith("-"):
                break
            roots.append(arg)
        depth = int(cmd[cmd.index("-maxdepth") + 1])
        names = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-name"]
        out = []
        for root in roots:
            local = self._helper_local(root, binds, volumes_from)
            if local is None or not local.exists():
                continue
            for dirpath, _dirs, files in os.walk(local):
                rel = Path(dirpath).relative_to(local)
                if len(rel.parts) >= depth:
                    continue
                for f in files:
                    if any(fnmatch.fnmatchcase(f, n) for n in names):
                        out.append(f"{root.rstrip('/')}/{(rel / f).as_posix()}"
                                   .replace("/./", "/"))
        return ExecResult(0, "".join(f"{line}\n" for line in sorted(out)))

    def _helper_game_copy(self, cmd: list[str], binds: dict[str, str]) -> ExecResult:
        """The importer's game-file copy: $1's entries into /out, minus saves and such."""
        out = self._helper_local("/out", binds, [])
        src = self._helper_local(cmd[4], binds, [])
        if out is None or src is None or not src.is_dir():
            return ExecResult(1, f"sh: cd: can't cd to {cmd[4]}\n")
        skip = {"savegame", "logs", "backups", "crash_dumps", "enshrouded_server.json", cmd[5]}
        for entry in src.iterdir():
            if entry.name in skip:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, out / entry.name, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, out / entry.name, follow_symlinks=False)
        return ExecResult(0, "")

    def _helper_copy(self, cmd: list[str], binds: dict[str, str]) -> ExecResult:
        """The importer's copy script: save folder ($1) and config ($2) into /out."""
        out = self._helper_local("/out", binds, [])
        save = self._helper_local(cmd[4], binds, [])
        config = self._helper_local(cmd[5], binds, []) if len(cmd) > 5 and cmd[5] else None
        if out is None or save is None or not save.is_dir():
            return ExecResult(1, f"cp: can't stat '{cmd[4]}'\n")
        shutil.copytree(save, out / "savegame", dirs_exist_ok=True)
        if config is not None:
            shutil.copy2(config, out / "enshrouded_server.json")
        return ExecResult(0, "")

    async def stats(self, name_or_id: str) -> ContainerStats | None:
        c = self._find(name_or_id)
        if not c or c.status != "running":
            return None
        uptime = (datetime.now(UTC) - (c.started_at or datetime.now(UTC))).total_seconds()
        return ContainerStats(cpu_pct=12.5, mem_bytes=6 * 1024**3, mem_limit=16 * 1024**3,
                              net_rx=int(uptime * 20_000), net_tx=int(uptime * 50_000))

    async def local_digest(self, image: str) -> str | None:
        return self.local_digests.get(image, "sha256:" + "a" * 64)

    async def registry_digest(self, image: str) -> str | None:
        return self.registry_digests.get(image, self.local_digests.get(image, "sha256:" + "a" * 64))


FAKE_APP_INFO = """AppID : 2278520, change number : 1/1
"2278520"
{
\t"depots"
\t{
\t\t"branches"
\t\t{
\t\t\t"public"
\t\t\t{
\t\t\t\t"buildid"\t\t"BUILD"
\t\t\t}
\t\t}
\t}
}
"""
