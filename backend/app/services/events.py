"""World event timeline."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.models import Event
from app.services.bus import Bus

log = logging.getLogger(__name__)

EVENT_KINDS = {
    "started",
    "stopped",
    "crashed",
    "online",
    "config_saved",
    "config_applied",
    "update_available",
    "updated",
    "backup_created",
    "restored",
    "player_joined",
    "player_left",
    "warning",
    "created",
    "adopted",
}


def event_to_dict(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "world_id": e.world_id,
        "ts": e.ts.isoformat(),
        "kind": e.kind,
        "data": e.data or {},
    }


class EventLog:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        # Called with each recorded event (notifications). Must not block or raise.
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def record(self, world_id: str, kind: str, **data: Any) -> dict[str, Any]:
        assert kind in EVENT_KINDS, kind
        with session_scope() as s:
            e = Event(world_id=world_id, kind=kind, data=data)
            s.add(e)
            s.flush()
            out = event_to_dict(e)
        self.bus.publish(f"events:{world_id}", out)
        for listener in self.listeners:
            try:
                listener(out)
            except Exception:
                log.exception("event listener failed")
        return out

    def recent(self, world_id: str, limit: int = 50, *, kinds: list[str] | None = None,
               before: int | None = None) -> list[dict[str, Any]]:
        q = select(Event).where(Event.world_id == world_id)
        if kinds:
            q = q.where(Event.kind.in_(kinds))
        if before is not None:
            q = q.where(Event.id < before)
        with session_scope() as s:
            rows = s.scalars(q.order_by(Event.id.desc()).limit(limit)).all()
            return [event_to_dict(e) for e in rows]
