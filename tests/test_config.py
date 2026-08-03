"""Configuration loading and required environment variables."""

from __future__ import annotations

import pytest

from app.config import Settings


def _set_required_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")


def test_icecast_source_password_is_required_from_env(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ICECAST_SOURCE_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="ICECAST_SOURCE_PASSWORD"):
        Settings.from_env()


def test_icecast_source_password_is_loaded_from_env(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-secret")

    settings = Settings.from_env()

    assert settings.icecast_source_password == "test-secret"
