"""Query-port allocation.

The controller runs in its own network namespace, so a local test bind would say
nothing about the host. Host UDP ports in use are read from /proc/net/udp{,6} in a
short-lived helper container on the host network (or directly, outside a container).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings
from app.services.docker_adapter import DockerAdapter

log = logging.getLogger(__name__)

MAX_PORT = 65535


def parse_proc_net_udp(text: str) -> set[int]:
    ports: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[1] or parts[0] == "sl":
            continue
        try:
            ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        except ValueError:
            continue
    return ports


def suggest_port(used: set[int], start: int = 15637) -> int:
    port = start
    while port in used:
        port += 1
        if port > MAX_PORT:
            raise ValueError("no free port")
    return port


async def host_udp_ports_in_use(docker: DockerAdapter, settings: Settings) -> set[int]:
    if not settings.in_container and not settings.fake_docker:
        text = ""
        for p in ("/proc/net/udp", "/proc/net/udp6"):
            try:
                text += Path(p).read_text()
            except OSError:
                pass
        return parse_proc_net_udp(text)
    try:
        res = await docker.run_helper(
            settings.helper_image,
            ["cat", "/proc/net/udp", "/proc/net/udp6"],
            network_mode="host",
        )
        return parse_proc_net_udp(res.output)
    except Exception as e:  # helper unavailable: fall back to DB-only allocation
        log.warning("could not read host UDP ports: %s", e)
        return set()
