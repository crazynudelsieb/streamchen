"""Liveness.

Instance metadata used to be served from here as JSON for a client that
assembled the footer itself. The pages are rendered server-side now and get all
of it from ``web.build_templates``, so what is left is the health check.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__

router = APIRouter(tags=["meta"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "version": __version__}
