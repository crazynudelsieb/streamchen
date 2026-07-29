"""The instance metadata the footer renders."""

from __future__ import annotations

import pytest

from app import __version__
from app.config import Settings


async def test_meta_reports_the_running_version(client):
    payload = (await client.get("/api/meta")).json()
    assert payload["version"] == __version__
    assert payload["app_name"] == "streamchen"


async def test_the_license_contact_is_never_a_literal_address(client):
    payload = (await client.get("/api/meta")).json()
    assert payload["license_email"] == ["appchen", "outlook.at"]
    assert "appchen@outlook.at" not in (await client.get("/api/meta")).text


async def test_health_check_answers(client):
    response = await client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_bootstrapping_meta_hands_out_both_cookies(api):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        await client.get("/api/meta")
        assert client.cookies["sc_session"]
        assert client.cookies["sc_csrf"]


class TestConfiguredInstance:
    @pytest.fixture
    def settings(self) -> Settings:
        return Settings(
            database_url="sqlite+aiosqlite://",
            redis_url="redis://localhost:6379/0",
            base_url="http://test",
            contact_email="hello@example.com",
            contact_github="crazynudelsieb",
            imprint_name="Example Operator",
            imprint_address="Example Street 1\n1010 Vienna",
            data_location="Austria",
        )

    async def test_configured_channels_reach_the_footer(self, client):
        payload = (await client.get("/api/meta")).json()

        assert payload["contact_email"] == ["hello", "example.com"]
        assert [link["key"] for link in payload["contact_links"]] == ["github"]
        assert payload["imprint_enabled"] is True
        assert payload["data_location"] == "Austria"

    async def test_the_imprint_page_gets_its_details(self, client):
        payload = (await client.get("/api/imprint")).json()

        assert payload["name"] == "Example Operator"
        assert payload["address"].splitlines() == ["Example Street 1", "1010 Vienna"]
        assert payload["email"] == ["hello", "example.com"]
