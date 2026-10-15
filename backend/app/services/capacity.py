"""Host capacity vs Keen's guidance per Enshrouded server: 16 GB RAM,
6 CPU cores and 30 GB disk each. Shown as warnings, never as a block."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

RAM_PER_SERVER = 16 * 1024**3
CORES_PER_SERVER = 6
DISK_PER_SERVER = 30 * 1024**3
GAME_DOWNLOAD = 10 * 1024**3


def _mem_total() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def capacity(data_dir: Path, worlds: int, extra: int = 0, *, new_downloads: int = 0,
             mem_total: int | None = None, cores: int | None = None) -> dict[str, Any]:
    """`worlds` existing worlds plus `extra` planned ones; `new_downloads` of the planned
    ones download the game (the rest copy it from another world, same disk use)."""
    n = worlds + extra
    mem = mem_total if mem_total is not None else _mem_total()
    cpus = cores if cores is not None else os.cpu_count()
    usage = shutil.disk_usage(data_dir) if data_dir.exists() else None
    warnings: list[str] = []
    if n and mem and mem < n * RAM_PER_SERVER:
        warnings.append(
            f"{n} world{'s' if n != 1 else ''} would want {n * 16} GB of RAM by Keen's "
            f"guidance (16 GB each); this host has {mem / 1024**3:.0f} GB. Idle servers use far "
            "less, but busy ones can run out of memory.")
    if n and cpus and cpus < n * CORES_PER_SERVER:
        warnings.append(f"{n} world{'s' if n != 1 else ''} would want {n * CORES_PER_SERVER} CPU "
                        f"cores by Keen's guidance (6 each); this host has {cpus}.")
    if usage and extra and usage.free < extra * GAME_DOWNLOAD + 5 * 1024**3:
        warnings.append(f"Only {usage.free / 1024**3:.0f} GB free on the data disk; each new "
                        "world needs about 10 GB for the game, plus saves and backups.")
    elif usage and n and usage.total < n * DISK_PER_SERVER:
        warnings.append(f"Keen recommends 30 GB of disk per server; the data disk has "
                        f"{usage.total / 1024**3:.0f} GB in total for {n}.")
    return {
        "worlds": n,
        "mem_total": mem,
        "cpus": cpus,
        "disk_free": usage.free if usage else None,
        "disk_total": usage.total if usage else None,
        "recommended": {"ram": n * RAM_PER_SERVER, "cores": n * CORES_PER_SERVER,
                        "disk": n * DISK_PER_SERVER},
        "warnings": warnings,
        "download_bytes": new_downloads * GAME_DOWNLOAD,
    }
