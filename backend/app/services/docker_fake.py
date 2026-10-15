"""In-memory DockerAdapter for tests and UI development (FAKE_DOCKER=true)."""

from __future__ import annotations

import asyncio
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
        self._followers: dict[str, list[asyncio.Queue[str | None]]] = {}

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

    async def run_helper(
        self,
        image: str,
        cmd: list[str],
        binds: dict[str, str] | None = None,
        network_mode: str | None = None,
    ) -> ExecResult:
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
