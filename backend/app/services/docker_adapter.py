"""Docker access behind one interface so services can be tested against a fake.

The docker SDK is blocking; every call runs in a worker thread so the event loop is
never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anyio

from app.config import LABEL_MANAGED

log = logging.getLogger(__name__)

ProgressCb = Callable[[int | None, str], None]


@dataclass
class ContainerSpec:
    name: str
    image: str
    hostname: str
    labels: dict[str, str]
    env: dict[str, str]
    # "15637/udp" -> (host_ip, host_port)
    ports: dict[str, tuple[str, int]]
    # host_path -> container_path
    binds: dict[str, str]
    devices: list[str] = field(default_factory=list)
    stop_timeout: int = 90
    nano_cpus: int | None = None
    mem_limit_bytes: int | None = None
    network: str | None = None


@dataclass
class Mount:
    type: str  # bind | volume | tmpfs | ...
    destination: str
    source: str = ""  # host path (for a volume: its folder under /var/lib/docker)
    name: str | None = None  # volume name
    rw: bool = True


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    labels: dict[str, str]
    status: str  # created | running | paused | restarting | exited | dead
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    restart_count: int = 0
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    # "15637/udp" -> host ports it is published on
    ports: dict[str, list[int]] = field(default_factory=dict)
    restart_policy: str = ""
    network_mode: str = ""
    auto_remove: bool = False

    @property
    def running(self) -> bool:
        return self.status in ("running", "restarting")


@dataclass
class ExecResult:
    exit_code: int
    output: str


class DockerError(RuntimeError):
    pass


@dataclass
class ContainerStats:
    cpu_pct: float | None  # of one core = 100%
    mem_bytes: int | None
    mem_limit: int | None
    net_rx: int  # cumulative bytes
    net_tx: int


class DockerAdapter(Protocol):
    async def ping(self) -> dict[str, Any]: ...
    async def get_container(self, name_or_id: str) -> ContainerInfo | None: ...
    async def list_managed(self) -> list[ContainerInfo]: ...
    async def list_containers(self) -> list[ContainerInfo]: ...
    async def create_container(self, spec: ContainerSpec) -> ContainerInfo: ...
    async def start(self, name_or_id: str) -> None: ...
    async def stop(self, name_or_id: str, timeout: int) -> None: ...
    async def restart(self, name_or_id: str, timeout: int) -> None: ...
    async def remove(self, name_or_id: str, force: bool = False) -> None: ...
    async def update_restart_policy(self, name_or_id: str, policy: str) -> None: ...
    async def get_archive(self, name_or_id: str, path: str, dest: Path) -> None: ...
    async def path_exists(self, name_or_id: str, path: str) -> bool: ...
    async def copy_tree_out(self, name_or_id: str, path: str, dest: Path, *,
                            skip_top: set[str], on_bytes: Callable[[int], None]) -> int: ...
    async def remove_volume(self, name: str) -> None: ...
    async def volume_users(self, name: str) -> list[str]: ...
    async def exec(self, name_or_id: str, cmd: list[str]) -> ExecResult: ...
    async def logs_tail(
        self, name_or_id: str, tail: int = 500, since: datetime | None = None
    ) -> list[str]: ...
    def logs_follow(
        self,
        name_or_id: str,
        since: datetime | None = None,
        tail: int | None = 0,
        timestamps: bool = False,
    ) -> AsyncIterator[str]: ...
    async def image_exists(self, image: str) -> bool: ...
    async def pull(self, image: str, progress: ProgressCb | None = None) -> None: ...
    async def ensure_network(self, name: str) -> None: ...
    async def connect_network(self, network: str, name_or_id: str) -> None: ...
    async def run_helper(
        self,
        image: str,
        cmd: list[str],
        binds: dict[str, str] | None = None,
        network_mode: str | None = None,
        volumes_from: list[str] | None = None,
        timeout: int = 120,
    ) -> ExecResult: ...
    async def run_detached(
        self,
        name: str,
        image: str,
        cmd: list[str],
        *,
        labels: dict[str, str],
        privileged: bool = False,
        pid_host: bool = False,
    ) -> ContainerInfo: ...
    async def stats(self, name_or_id: str) -> ContainerStats | None: ...
    async def local_digest(self, image: str) -> str | None: ...
    async def registry_digest(self, image: str) -> str | None: ...


def _parse_ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    # Docker uses RFC3339 with nanoseconds: trim to microseconds.
    value = value.rstrip("Z")
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def split_timestamp(line: str) -> tuple[datetime | None, str]:
    """Split a line produced with timestamps=True into (time, text)."""
    head, sep, rest = line.partition(" ")
    if not sep:
        return None, line
    try:
        return _parse_ts(head), rest
    except ValueError:
        return None, line


class _ChunkReader:
    """A file-like reader over an iterator of byte chunks (for streaming tarfile)."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buf) < size:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        if size < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:size], self._buf[size:]
        return out


def _published_ports(host_config: dict[str, Any]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for port, bindings in (host_config.get("PortBindings") or {}).items():
        for b in bindings or []:
            try:
                out.setdefault(port, []).append(int(b.get("HostPort") or port.split("/")[0]))
            except ValueError:
                continue
    return out


def _split_lines(buffer: str, chunk: str) -> tuple[list[str], str]:
    buffer += chunk
    *lines, rest = buffer.split("\n")
    return [ln.rstrip("\r") for ln in lines], rest


class RealDockerAdapter:
    """DockerAdapter backed by the docker SDK and the local socket."""

    def __init__(self) -> None:
        import docker

        self._docker = docker
        self.client = docker.from_env(timeout=120)

    # -- helpers -----------------------------------------------------------------

    def _info(self, c: Any) -> ContainerInfo:
        attrs = c.attrs
        state = attrs.get("State", {})
        host = attrs.get("HostConfig") or {}
        env = {}
        for item in attrs.get("Config", {}).get("Env") or []:
            k, _, v = item.partition("=")
            env[k] = v
        return ContainerInfo(
            id=c.id,
            name=c.name,
            image=attrs.get("Config", {}).get("Image", ""),
            labels=attrs.get("Config", {}).get("Labels") or {},
            status=state.get("Status", "unknown"),
            exit_code=state.get("ExitCode"),
            started_at=_parse_ts(state.get("StartedAt")),
            finished_at=_parse_ts(state.get("FinishedAt")),
            restart_count=attrs.get("RestartCount", 0),
            env=env,
            mounts=[Mount(type=m.get("Type", ""), destination=m.get("Destination", ""),
                          source=m.get("Source", ""), name=m.get("Name"),
                          rw=bool(m.get("RW", True)))
                    for m in attrs.get("Mounts") or []],
            ports=_published_ports(host),
            restart_policy=(host.get("RestartPolicy") or {}).get("Name") or "no",
            network_mode=host.get("NetworkMode") or "",
            auto_remove=bool(host.get("AutoRemove")),
        )

    def _get(self, name_or_id: str) -> Any:
        try:
            return self.client.containers.get(name_or_id)
        except self._docker.errors.NotFound:
            return None

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except self._docker.errors.APIError as e:
            raise DockerError(str(e.explanation or e)) from e

    # -- API ---------------------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        return await self._run(self.client.version)

    async def get_container(self, name_or_id: str) -> ContainerInfo | None:
        def _do() -> ContainerInfo | None:
            c = self._get(name_or_id)
            return self._info(c) if c else None

        return await self._run(_do)

    async def list_managed(self) -> list[ContainerInfo]:
        def _do() -> list[ContainerInfo]:
            cs = self.client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"})
            return [self._info(c) for c in cs]

        return await self._run(_do)

    async def list_containers(self) -> list[ContainerInfo]:
        return await self._run(lambda: [self._info(c)
                                        for c in self.client.containers.list(all=True)])

    async def create_container(self, spec: ContainerSpec) -> ContainerInfo:
        def _do() -> ContainerInfo:
            # The low-level API is used because the high-level one has no stop_timeout.
            api = self.client.api
            host_config = api.create_host_config(
                port_bindings={k: (ip, port) for k, (ip, port) in spec.ports.items()},
                binds={h: {"bind": c, "mode": "rw"} for h, c in spec.binds.items()},
                restart_policy={"Name": "unless-stopped"},
                devices=[f"{d}:{d}:rwm" for d in spec.devices] or None,
                nano_cpus=spec.nano_cpus,
                mem_limit=spec.mem_limit_bytes,
                log_config={"Type": "json-file", "Config": {"max-size": "20m", "max-file": "5"}},
                network_mode=spec.network,
            )
            created = api.create_container(
                spec.image,
                name=spec.name,
                hostname=spec.hostname,
                labels=spec.labels,
                environment=spec.env,
                ports=[(int(k.split("/")[0]), k.split("/")[1]) for k in spec.ports],
                stop_timeout=spec.stop_timeout,
                host_config=host_config,
            )
            return self._info(self.client.containers.get(created["Id"]))

        return await self._run(_do)

    async def start(self, name_or_id: str) -> None:
        await self._run(lambda: self.client.containers.get(name_or_id).start())

    async def stop(self, name_or_id: str, timeout: int) -> None:
        await self._run(lambda: self.client.containers.get(name_or_id).stop(timeout=timeout))

    async def restart(self, name_or_id: str, timeout: int) -> None:
        await self._run(lambda: self.client.containers.get(name_or_id).restart(timeout=timeout))

    async def remove(self, name_or_id: str, force: bool = False) -> None:
        def _do() -> None:
            c = self._get(name_or_id)
            if c:
                c.remove(force=force)

        await self._run(_do)

    async def update_restart_policy(self, name_or_id: str, policy: str) -> None:
        await self._run(lambda: self.client.containers.get(name_or_id).update(
            restart_policy={"Name": policy}))

    async def get_archive(self, name_or_id: str, path: str, dest: Path) -> None:
        """Copy `path` out of a container (running or not) as a tar file, like docker cp."""

        def _do() -> None:
            try:
                stream, _ = self.client.containers.get(name_or_id).get_archive(path)
            except self._docker.errors.NotFound as e:
                raise DockerError(f"{path} doesn't exist in the container") from e
            with open(dest, "wb") as out:
                for chunk in stream:
                    out.write(chunk)

        await self._run(_do)

    async def path_exists(self, name_or_id: str, path: str) -> bool:
        """Whether a file or folder exists in a container (running or not), without
        copying it (HEAD /containers/{id}/archive)."""

        def _do() -> bool:
            api = self.client.api
            res = api.head(api._url("/containers/{0}/archive", name_or_id),
                           params={"path": path})
            return res.status_code == 200

        return await self._run(_do)

    async def copy_tree_out(self, name_or_id: str, path: str, dest: Path, *,
                            skip_top: set[str], on_bytes: Callable[[int], None]) -> int:
        """Stream a folder out of a container into `dest` (like docker cp, without a
        temporary tar file)."""
        from app.services.backup_archive import extract_tree

        def _do() -> int:
            try:
                stream, _ = self.client.containers.get(name_or_id).get_archive(
                    path, chunk_size=1024 * 1024)
            except self._docker.errors.NotFound as e:
                raise DockerError(f"{path} doesn't exist in the container") from e
            return extract_tree(_ChunkReader(stream), dest, skip_top=skip_top,
                                on_bytes=on_bytes)

        return await self._run(_do)

    async def remove_volume(self, name: str) -> None:
        def _do() -> None:
            try:
                self.client.volumes.get(name).remove()
            except self._docker.errors.NotFound:
                pass

        await self._run(_do)

    async def volume_users(self, name: str) -> list[str]:
        return await self._run(lambda: [
            c.name for c in self.client.containers.list(all=True, filters={"volume": name})])

    async def exec(self, name_or_id: str, cmd: list[str]) -> ExecResult:
        def _do() -> ExecResult:
            res = self.client.containers.get(name_or_id).exec_run(cmd, demux=False)
            out = res.output.decode("utf-8", "replace") if res.output else ""
            return ExecResult(res.exit_code or 0, out)

        return await self._run(_do)

    async def logs_tail(
        self, name_or_id: str, tail: int = 500, since: datetime | None = None
    ) -> list[str]:
        def _do() -> list[str]:
            c = self._get(name_or_id)
            if not c:
                return []
            kwargs: dict[str, Any] = {"tail": tail}
            if since:
                kwargs["since"] = since
            raw = c.logs(**kwargs).decode("utf-8", "replace")
            return [ln.rstrip("\r") for ln in raw.splitlines()]

        return await self._run(_do)

    async def logs_follow(
        self,
        name_or_id: str,
        since: datetime | None = None,
        tail: int | None = 0,
        timestamps: bool = False,
    ) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=5000)
        holder: dict[str, Any] = {}
        stopped = threading.Event()

        def _reader() -> None:
            buffer = ""
            try:
                c = self.client.containers.get(name_or_id)
                kwargs: dict[str, Any] = {
                    "stream": True,
                    "follow": True,
                    "timestamps": timestamps,
                }
                if since is not None:
                    kwargs["since"] = since
                if tail is not None:
                    kwargs["tail"] = tail if tail > 0 else 0
                stream = c.logs(**kwargs)
                holder["stream"] = stream
                for chunk in stream:
                    if stopped.is_set():
                        break
                    lines, buffer = _split_lines(buffer, chunk.decode("utf-8", "replace"))
                    for line in lines:
                        loop.call_soon_threadsafe(_put, line)
                if buffer:
                    loop.call_soon_threadsafe(_put, buffer)
            except Exception as e:  # container removed, daemon gone, ...
                if not stopped.is_set():
                    log.debug("log follow for %s ended: %s", name_or_id, e)
            finally:
                loop.call_soon_threadsafe(_put, None)

        def _put(item: str | None) -> None:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass  # slow consumer: drop lines rather than block the reader

        thread = threading.Thread(target=_reader, daemon=True, name=f"logs-{name_or_id}")
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            stopped.set()
            stream = holder.get("stream")
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass

    async def image_exists(self, image: str) -> bool:
        def _do() -> bool:
            try:
                self.client.images.get(image)
                return True
            except self._docker.errors.ImageNotFound:
                return False

        return await self._run(_do)

    async def pull(self, image: str, progress: ProgressCb | None = None) -> None:
        repo, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "")
        tag = tag or "latest"

        def _do() -> None:
            layers: dict[str, tuple[int, int]] = {}
            last_pct = -1
            for ev in self.client.api.pull(repo, tag=tag, stream=True, decode=True):
                if "error" in ev:
                    raise DockerError(ev["error"])
                layer = ev.get("id")
                detail = ev.get("progressDetail") or {}
                status = ev.get("status", "")
                if layer and detail.get("total"):
                    if status.startswith("Downloading"):
                        layers[layer] = (detail.get("current", 0), detail["total"])
                elif layer and status in ("Download complete", "Pull complete", "Already exists"):
                    cur = layers.get(layer)
                    if cur:
                        layers[layer] = (cur[1], cur[1])
                if progress and layers:
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    pct = int(done * 100 / total) if total else None
                    if pct is not None and pct != last_pct:
                        last_pct = pct
                        progress(pct, f"Pulling {image}: {pct}%")

        await self._run(_do)

    async def ensure_network(self, name: str) -> None:
        def _do() -> None:
            # Docker's name filter also matches partial names: "flamekeeper" finds the
            # compose project's "flamekeeper_default". Only an exact match counts.
            if not any(n.name == name for n in self.client.networks.list(names=[name])):
                self.client.networks.create(name, driver="bridge", labels={LABEL_MANAGED: "true"})

        await self._run(_do)

    async def connect_network(self, network: str, name_or_id: str) -> None:
        def _do() -> None:
            net = self.client.networks.get(network)
            c = self.client.containers.get(name_or_id)
            if network not in (c.attrs.get("NetworkSettings", {}).get("Networks") or {}):
                net.connect(c)

        await self._run(_do)

    async def run_helper(
        self,
        image: str,
        cmd: list[str],
        binds: dict[str, str] | None = None,
        network_mode: str | None = None,
        volumes_from: list[str] | None = None,
        timeout: int = 120,
    ) -> ExecResult:
        """Run a throwaway container (at most `timeout` seconds). A bind's container path
        may end in ":ro"."""

        def _do() -> ExecResult:
            try:
                self.client.images.get(image)
            except self._docker.errors.ImageNotFound:
                self.client.images.pull(image)
            volumes = {}
            for h, p in (binds or {}).items():
                target, ro = (p[:-3], True) if p.endswith(":ro") else (p, False)
                volumes[h] = {"bind": target, "mode": "ro" if ro else "rw"}
            c = self.client.containers.run(
                image,
                cmd,
                detach=True,
                volumes=volumes,
                volumes_from=volumes_from,
                network_mode=network_mode,
                labels={f"{LABEL_MANAGED}.helper": "true"},
            )
            try:
                result = c.wait(timeout=timeout)
                out = c.logs().decode("utf-8", "replace")
                return ExecResult(int(result.get("StatusCode", 1)), out)
            finally:
                c.remove(force=True)

        return await self._run(_do)

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
        """Start a container and return at once; it's left for the caller to remove."""

        def _do() -> ContainerInfo:
            c = self.client.containers.run(
                image, cmd, name=name, detach=True, labels=labels, privileged=privileged,
                pid_mode="host" if pid_host else None,
            )
            return self._info(self.client.containers.get(c.id))

        return await self._run(_do)

    async def stats(self, name_or_id: str) -> ContainerStats | None:
        def _do() -> ContainerStats | None:
            try:
                c = self.client.containers.get(name_or_id)
            except self._docker.errors.NotFound:
                return None
            if c.status != "running":
                return None
            return parse_stats(c.stats(stream=False))

        return await self._run(_do)

    async def local_digest(self, image: str) -> str | None:
        def _do() -> str | None:
            try:
                img = self.client.images.get(image)
            except self._docker.errors.ImageNotFound:
                return None
            repo = image.rsplit(":", 1)[0]
            for d in img.attrs.get("RepoDigests") or []:
                if d.split("@", 1)[0] == repo or d.startswith(repo + "@"):
                    return d.split("@", 1)[1]
            return None

        return await self._run(_do)

    async def registry_digest(self, image: str) -> str | None:
        def _do() -> str | None:
            data = self.client.images.get_registry_data(image)
            return data.id

        return await self._run(_do)


def parse_stats(s: dict[str, Any]) -> ContainerStats:
    """One `docker stats --no-stream` sample → CPU % (100 = one core), memory, network."""
    cpu = s.get("cpu_stats") or {}
    pre = s.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage", {}).get("total_usage", 0)
                 - pre.get("cpu_usage", {}).get("total_usage", 0))
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_pct = (cpu_delta / sys_delta) * cpus * 100 if sys_delta > 0 and cpu_delta >= 0 else None
    mem = s.get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is not None:
        # Like the docker CLI: don't count the page cache.
        stats = mem.get("stats") or {}
        usage -= stats.get("inactive_file", stats.get("total_inactive_file", 0))
    rx = tx = 0
    for n in (s.get("networks") or {}).values():
        rx += n.get("rx_bytes", 0)
        tx += n.get("tx_bytes", 0)
    return ContainerStats(cpu_pct=round(cpu_pct, 2) if cpu_pct is not None else None,
                          mem_bytes=usage, mem_limit=mem.get("limit"), net_rx=rx, net_tx=tx)
