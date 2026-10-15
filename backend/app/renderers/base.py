"""ConfigRenderer: how a world's server config reaches the game container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ConfigRenderer(ABC):
    mode: str
    # True when applying a new config needs a container recreate (env changes),
    # False when a process restart is enough (the server re-reads the file).
    requires_recreate: bool

    @abstractmethod
    def container_env(self, managed: dict[str, Any]) -> dict[str, str]:
        """Extra environment variables for the game container."""

    @abstractmethod
    def write(
        self,
        path: Path,
        managed: dict[str, Any],
        *,
        uid: int | None,
        gid: int | None,
        remove: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Persist the managed config; return the resulting full document.

        `remove` lists top-level keys to delete (only the legacy `password`).
        """
