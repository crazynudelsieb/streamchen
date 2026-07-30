"""Installability, and the schema surviving a release that adds a column.

Both are things that only go wrong in production: a service worker with the
wrong scope controls nothing, and a column that exists in the models and not in
the database breaks every query that names it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.database import create_engine, create_schema, create_sessionmaker
from app.models import Room
from app.security import hash_secret, new_room_token


# --- Service worker ---------------------------------------------------------
async def test_the_service_worker_is_served_from_the_root(client):
    """A worker may only control what is under its own path, so this one file
    cannot live under /static with everything else."""
    response = await client.get("/sw.js")

    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "javascript" in response.headers["content-type"]


async def test_the_service_worker_is_not_cached(client):
    """It is how every other cache gets replaced; a stale one is the worst
    kind of stale."""
    response = await client.get("/sw.js")

    assert "no-store" in response.headers["cache-control"]


async def test_the_service_worker_stays_away_from_live_state(client):
    """The rule that keeps a cached page from ever showing a stale room."""
    source = (await client.get("/sw.js")).text

    # Only content-addressed paths may be cached: versioned assets, and the
    # avatars drawn from their own seed.
    assert "/^\\/static\\//" in source
    assert "/^\\/a\\//" in source
    # Navigations go to the network first, always.
    assert "request.mode === 'navigate'" in source
    assert "fetch(request).catch" in source
    # And it is served intact, em dashes and all.
    assert "the live fragment — is left alone" in source


async def test_the_manifest_points_at_icons_that_exist(client, api):
    manifest = json.loads((await client.get("/static/site.webmanifest")).text)

    assert manifest["display"] == "standalone"
    assert manifest["start_url"].startswith("/")

    sizes = set()
    for icon in manifest["icons"]:
        response = await client.get(icon["src"])
        assert response.status_code == 200, icon["src"]
        sizes.add(icon["sizes"])

    # What an installable app is actually required to offer.
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


async def test_there_is_something_to_show_when_the_network_is_gone(client):
    response = await client.get("/static/offline.html")

    assert response.status_code == 200
    # Standalone by necessity: it is served when nothing else can be fetched.
    assert "<link" not in response.text
    assert "streamchen" in response.text


async def test_the_room_page_asks_to_be_installable(client, room):
    page = (await client.get(f"/r/{room['token']}")).text

    assert 'rel="manifest"' in page
    assert 'rel="apple-touch-icon"' in page
    assert 'name="theme-color"' in page


# --- Being offered the install ----------------------------------------------
async def test_every_page_carries_the_install_offer(client):
    """A button for people who go looking, and one banner for everyone else."""
    page = (await client.get("/")).text

    assert 'id="installButton"' in page
    assert 'id="installBanner"' in page
    assert 'id="installAccept"' in page
    assert 'id="installDismiss"' in page


async def test_the_install_button_starts_hidden(client):
    """It is shown only once the browser says the app can be installed, so it is
    never a button that does nothing."""
    page = (await client.get("/")).text

    assert 'class="btn btn-outline-secondary btn-sm d-none" type="button" id="installButton"' in page
    assert 'class="install-banner d-none" id="installBanner"' in page


async def test_an_installed_app_is_not_asked_to_install_again(client):
    page = (await client.get("/")).text

    assert "@media (display-mode: standalone)" in page
    assert ".install-banner, #installButton { display: none !important; }" in page


async def test_the_page_explains_the_install_where_a_browser_will_not_do_it(client):
    """Safari has no install prompt to defer: adding to the home screen is
    something the person does, so the modal says how."""
    page = (await client.get("/")).text

    assert 'id="installModal"' in page
    assert "Add to Home Screen" in page
    assert 'id="installStepsIos"' in page
    assert 'id="installStepsDesktop"' in page


async def test_the_browsers_own_install_bar_is_taken_over_rather_than_left(client):
    source = (await client.get("/static/app.js")).text

    # Kept for the button to use, instead of the browser's own unstyleable bar.
    assert "'beforeinstallprompt'" in source
    assert "event.preventDefault();" in source
    assert "deferred = event;" in source
    # And an install that happened elsewhere takes the offer away.
    assert "'appinstalled'" in source


async def test_not_now_is_remembered(client):
    """The banner is an offer, not a nag: once declined it stays declined."""
    source = (await client.get("/static/app.js")).text

    assert "streamchen:install-hint" in source
    assert "installHintSilenced" in source
    # iPadOS reports itself as a Mac, so the touch API is what tells them apart.
    assert "'ontouchend' in document" in source


# --- Additive schema changes ------------------------------------------------
@pytest.fixture
async def sqlite_engine():
    engine = create_engine("sqlite+aiosqlite://")
    await create_schema(engine)
    yield engine
    await engine.dispose()


async def test_a_column_added_by_a_release_lands_on_an_existing_database(sqlite_engine):
    """``create_all`` only creates tables it cannot find, so without this a
    release that adds a column leaves a live database without it."""
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE rooms DROP COLUMN stream_stopped"))
        columns = await conn.run_sync(
            lambda sync: [c["name"] for c in __import__("sqlalchemy").inspect(sync).get_columns("rooms")]
        )
    assert "stream_stopped" not in columns

    await create_schema(sqlite_engine)

    sessionmaker = create_sessionmaker(sqlite_engine)
    async with sessionmaker() as db:
        room = Room(token=new_room_token(), name="Back", host_secret_hash=hash_secret("k"))
        db.add(room)
        await db.commit()
        # The default came with the column, so existing rows have one too.
        assert room.stream_stopped is False


async def test_reconciling_a_schema_that_needs_nothing_changes_nothing(sqlite_engine):
    """It runs on every start-up; doing so must be free and silent."""
    async with sqlite_engine.begin() as conn:
        before = await conn.run_sync(
            lambda sync: {
                table: [c["name"] for c in __import__("sqlalchemy").inspect(sync).get_columns(table)]
                for table in ("rooms", "listeners", "tracks", "votes", "bans")
            }
        )

    await create_schema(sqlite_engine)

    async with sqlite_engine.begin() as conn:
        after = await conn.run_sync(
            lambda sync: {
                table: [c["name"] for c in __import__("sqlalchemy").inspect(sync).get_columns(table)]
                for table in ("rooms", "listeners", "tracks", "votes", "bans")
            }
        )

    assert before == after
