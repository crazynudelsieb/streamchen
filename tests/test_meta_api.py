"""Liveness, and the instance metadata that reaches a page.

The metadata itself is asserted where it is rendered (``test_web_pages.py``):
the pages get it from the Jinja globals, and there is no JSON copy of it.
"""

from __future__ import annotations

from app import __version__


async def test_health_check_answers(client):
    response = await client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == __version__


async def test_static_urls_carry_the_running_version(client):
    """What makes the immutable cache header on /static/ safe across releases."""
    assert f"?v={__version__}" in (await client.get("/")).text


async def test_the_license_contact_is_never_a_literal_address(client):
    page = (await client.get("/")).text

    assert "appchen@outlook.at" not in page
    assert 'data-user="appchen"' in page
    assert 'data-domain="outlook.at"' in page


async def test_a_first_request_hands_out_both_cookies(api):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        await client.get("/api/healthz")

        assert client.cookies["sc_session"]
        assert client.cookies["sc_csrf"]
