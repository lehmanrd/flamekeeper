"""Startup self-check.

The controller creates sibling containers through the host daemon, so bind-mount
sources are host paths. DATA_DIR must therefore be mounted at the same path inside
the controller. That is verified by writing a marker file and checking for it from a
throwaway container that bind-mounts DATA_DIR.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from app.config import GAME_IMAGE_REPO, Settings
from app.services.docker_adapter import DockerAdapter

log = logging.getLogger(__name__)

MIN_FREE_BYTES = 30 * 1024**3


@dataclass
class Check:
    id: str
    label: str
    status: str  # ok | warn | fail | skip
    detail: str
    fatal: bool = False


@dataclass
class SelfCheckResult:
    checks: list[Check]
    docker_version: str | None = None
    ntsync_available: bool = False

    @property
    def fatal_error(self) -> str | None:
        for c in self.checks:
            if c.fatal and c.status == "fail":
                return c.detail
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [asdict(c) for c in self.checks],
            "docker_version": self.docker_version,
            "ntsync_available": self.ntsync_available,
            "fatal_error": self.fatal_error,
        }


async def run_selfcheck(settings: Settings, docker: DockerAdapter) -> SelfCheckResult:
    checks: list[Check] = []
    result = SelfCheckResult(checks)

    # Docker socket
    try:
        version = await docker.ping()
        result.docker_version = version.get("Version")
        checks.append(Check("docker", "Docker socket", "ok",
                            f"Docker {version.get('Version')} (API {version.get('ApiVersion')})",
                            fatal=True))
    except Exception as e:
        checks.append(Check("docker", "Docker socket", "fail",
                            f"Cannot reach the Docker daemon: {e}. Mount /var/run/docker.sock.",
                            fatal=True))
        return result

    # DATA_DIR exists and is writable
    data_dir = settings.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        settings.worlds_dir.mkdir(parents=True, exist_ok=True)
        marker = data_dir / f".flamekeeper-path-check-{uuid.uuid4().hex}"
        marker.write_text("ok")
    except OSError as e:
        checks.append(Check("data_dir", "DATA_DIR writable", "fail",
                            f"{data_dir} is not writable: {e}", fatal=True))
        return result
    checks.append(Check("data_dir", "DATA_DIR writable", "ok", str(data_dir)))

    # DATA_DIR path match
    try:
        if settings.skip_path_check:
            checks.append(Check("path_match", "DATA_DIR path match", "skip",
                                "Skipped (SKIP_PATH_CHECK=true)"))
        elif not settings.in_container and not settings.fake_docker:
            checks.append(Check("path_match", "DATA_DIR path match", "ok",
                                f"{data_dir} (controller runs on the host)"))
        else:
            res = await docker.run_helper(
                settings.helper_image,
                ["sh", "-c", f"test -f {marker}"],
                binds={str(data_dir): str(data_dir)},
            )
            if res.exit_code == 0:
                checks.append(Check("path_match", "DATA_DIR path match", "ok", str(data_dir),
                                    fatal=True))
            else:
                checks.append(Check(
                    "path_match", "DATA_DIR path match", "fail",
                    f"DATA_DIR={data_dir} is not mounted at the same path inside and outside "
                    f"the controller. Use a volume like '{data_dir}:{data_dir}'.",
                    fatal=True,
                ))
    except Exception as e:
        checks.append(Check("path_match", "DATA_DIR path match", "fail",
                            f"Could not run the path check container: {e}", fatal=True))
    finally:
        marker.unlink(missing_ok=True)

    # Disk
    usage = shutil.disk_usage(data_dir)
    free_gb = usage.free / 1024**3
    pct_free = usage.free * 100 / usage.total if usage.total else 0
    status = "ok" if usage.free >= MIN_FREE_BYTES and pct_free >= 10 else "warn"
    checks.append(Check("disk", "Disk free", status,
                        f"{free_gb:.0f} GB free ({pct_free:.0f}%) on {data_dir}. "
                        "Recommended: 30 GB per world."))

    # ntsync (kernel >= 6.14): look at the host's /dev via a helper container.
    try:
        if settings.in_container or settings.fake_docker:
            res = await docker.run_helper(settings.helper_image,
                                          ["sh", "-c", "test -c /hostdev/ntsync"],
                                          binds={"/dev": "/hostdev"})
            result.ntsync_available = res.exit_code == 0
        else:
            from pathlib import Path

            result.ntsync_available = Path("/dev/ntsync").exists()
    except Exception:
        result.ntsync_available = False
    checks.append(Check("ntsync", "ntsync", "ok" if result.ntsync_available else "skip",
                        "/dev/ntsync available" if result.ntsync_available
                        else "Not available (needs kernel 6.14+); optional"))

    # Game image reachable (local copy or registry)
    image = f"{GAME_IMAGE_REPO}:{settings.default_image_tag}"
    try:
        if await docker.image_exists(image):
            checks.append(Check("image", "Game image", "ok", f"{image} present locally"))
        else:
            checks.append(Check("image", "Game image", "warn",
                                f"{image} not pulled yet; it will be pulled on first world "
                                "creation"))
    except Exception as e:
        checks.append(Check("image", "Game image", "warn", f"Could not check {image}: {e}"))

    return result
