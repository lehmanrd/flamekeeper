"""In-process pub/sub used to fan out live updates to SSE clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Any]]] = {}

    def publish(self, topic: str, data: Any) -> None:
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow subscriber; it will resync on reconnect

    @asynccontextmanager
    async def subscribe(self, topic: str, maxsize: int = 1000) -> AsyncIterator[asyncio.Queue]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(topic, set()).add(q)
        try:
            yield q
        finally:
            subs = self._subs.get(topic)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    del self._subs[topic]

    def has_subscribers(self, topic: str) -> bool:
        return bool(self._subs.get(topic))
