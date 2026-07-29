"""ASGI entry point: ``uvicorn app.api.main:app``."""

from __future__ import annotations

from app.api import create_app

app = create_app()
