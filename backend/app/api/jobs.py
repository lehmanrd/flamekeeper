"""Job list, detail, cancel."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.deps import get_services
from app.db import session_scope
from app.models import Job
from app.services.container import Services
from app.services.jobs import job_to_dict

router = APIRouter(prefix="/jobs")


@router.get("")
async def list_jobs(
    world_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if world_id:
            q = q.where(Job.world_id == world_id)
        if status:
            q = q.where(Job.status.in_(status.split(",")))
        return [job_to_dict(j) for j in s.scalars(q)]


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return {**job_to_dict(job), "log": job.log}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, services: Services = Depends(get_services)) -> dict:
    if not services.jobs.cancel(job_id):
        raise HTTPException(409, "Job is not running")
    return {"cancelled": True}
