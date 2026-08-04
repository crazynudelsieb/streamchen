"""The rendered pages: what a browser actually receives."""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.models import STATE_PLAYED, STATE_PLAYING, Track, utcnow
from app.web import format_ago, format_duration, format_score
from tests.conftest import video_id, watch_url


# --- Template filters -------------------------------------------------------
def test_duration_is_minutes_and_seconds():
    assert format_duration(187) == "3:07"
    assert format_duration(59) == "0:59"


def test_duration_grows_an_hours_field_only_when_needed():
    assert format_duration(3600) == "1:00:00"
    assert format_duration(3753) == "1:02:33"


def test_unknown_durations_read_as_zero():
    for value in (0, -5, None):
        assert format_duration(value) == "0:00"


def test_score_keeps_the_sign_visible():
    assert format_score(3) == "+3"
    assert format_score(0) == "0"
    assert format_score(-2) == "-2"


def test_ago_says_nothing_about_a_missing_timestamp():
    assert format_ago(None) == ""


# --- Landing page -----------------------------------------------------------
async def test_the_landing_page_renders(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Everyone’s radio." in response.text
    assert "createRoomForm" in response.text


async def test_pages_are_never_cached(client):
    assert (await client.get("/")).headers["cache-control"] == "no-store"


async def test_static_assets_are_cached_forever(client):
    response = await client.get("/static/app.js")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


async def test_vendored_assets_are_served_from_this_server(client):
    """No CDN: every asset the page references comes from here."""
    page = await client.get("/")

    assert "cdn.jsdelivr.net" not in page.text
    assert "fonts.googleapis.com" not in page.text
    assert (await client.get("/static/vendor/bootstrap/bootstrap.min.css")).status_code == 200
    assert (await client.get("/static/vendor/inter/inter.css")).status_code == 200


async def test_the_footer_carries_the_licence_and_never_a_literal_address(client):
    page = await client.get("/")

    assert "commercial use requires permission" in page.text
    assert "appchen@outlook.at" not in page.text
    assert 'data-user="appchen"' in page.text
    assert 'data-domain="outlook.at"' in page.text
    assert "mailto:" not in page.text


async def test_the_landing_page_is_indexable_but_rooms_are_not(client, room):
    assert "noindex" in (await client.get("/")).text
    assert "noindex" in (await client.get(f"/r/{room['token']}")).text


# --- Room page --------------------------------------------------------------
async def test_the_room_page_renders_the_stream_and_the_host_controls(client, room):
    response = await client.get(f"/r/{room['token']}")

    assert response.status_code == 200
    assert "Test Room" in response.text
    assert f'data-stream="http://stream.test/{room["token"]}.mp3"' in response.text
    # Host controls are a button and a dialog now, not a card in the way.
    assert 'id="hostModal"' in response.text
    assert 'id="audio"' in response.text


async def test_the_room_page_offers_to_change_your_name(client, room):
    response = await client.get(f"/r/{room['token']}")

    assert 'id="nameEdit"' in response.text
    assert 'id="nameInput"' in response.text
    # The way back to a generated name, so the first edit is not final.
    assert 'id="nameShuffle"' in response.text
    # And the other half of who you are here: a different cat, in the same menu.
    assert 'id="avatarShuffle"' in response.text


async def test_the_name_menu_shows_the_cat_it_would_replace(client, room):
    """The button is the avatar, so both it and the header show the one in use
    -- including after a re-roll, which the page must not still be ignoring."""
    chosen = (await client.post(f"/api/rooms/{room['token']}/me/avatar")).json()["avatar"]

    response = await client.get(f"/r/{room['token']}")

    assert response.text.count(f'src="/a/{chosen}.svg"') == 2


async def test_the_queue_shows_the_cat_a_submitter_chose(client, room):
    """The rows are rendered from a seed the server resolved, not from the
    submitter's id -- an id can no longer say which cat is theirs."""
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})
    chosen = (await client.post(f"/api/rooms/{room['token']}/me/avatar")).json()["avatar"]

    fragment = await client.get(f"/r/{room['token']}/live")

    assert f'src="/a/{chosen}.svg"' in fragment.text


async def test_the_name_control_is_outside_the_swapped_region(client, room):
    """The header is not part of the live fragment: re-rendering it on every
    playback event would close the form under whoever was typing in it."""
    fragment = await client.get(f"/r/{room['token']}/live")

    assert fragment.status_code == 200
    assert 'id="nameEdit"' not in fragment.text


async def test_a_listener_gets_no_host_controls(new_client, room):
    guest = await new_client()
    response = await guest.get(f"/r/{room['token']}")

    assert response.status_code == 200
    assert 'id="hostModal"' not in response.text
    assert 'id="claimModal"' in response.text


async def test_the_listener_count_opens_who_is_here(new_client, room):
    """Not a host control: everybody in the room may see who else is."""
    guest = await new_client()
    response = await guest.get(f"/r/{room['token']}")

    assert 'data-bs-target="#listenersModal"' in response.text
    assert 'id="listenersModal"' in response.text
    assert 'id="roomListenerList"' in response.text


async def test_queued_songs_appear_in_the_page(client, room):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})

    response = await client.get(f"/r/{room['token']}")

    assert f"Track {video_id(1)}" in response.text
    assert 'data-action="vote"' in response.text


async def test_an_empty_queue_says_so(client, room):
    assert "Nothing queued" in (await client.get(f"/r/{room['token']}")).text


# --- Recently played --------------------------------------------------------
async def _set_state(api, track_id: str, state: str) -> None:
    """Move a track along its lifecycle the way the worker would, without one."""
    async with api.state.sessionmaker() as db:
        track = await db.get(Track, uuid.UUID(track_id))
        track.state = state
        if state == STATE_PLAYING:
            track.started_at = utcnow()
        else:
            track.ended_at = utcnow()
        await db.commit()


async def _play(api, client, room, seed: int) -> dict:
    """Queue a song and let it finish, so it is in the history."""
    track = (
        await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(seed)})
    ).json()
    await _set_state(api, track["id"], STATE_PLAYED)
    return track


async def test_a_played_song_can_be_queued_again_from_the_history(api, client, room):
    await _play(api, client, room, 31)

    page = await client.get(f"/r/{room['token']}")

    assert 'data-action="requeue"' in page.text
    assert f'data-youtube="{video_id(31)}"' in page.text


async def test_the_history_offers_a_song_back_once_until_it_has_played_again(api, client, room):
    """The whole rule. Re-adding is what takes the offer away, and only the song
    playing again brings it back — otherwise this would be a loop button."""
    await _play(api, client, room, 32)

    again = await client.post(
        f"/api/rooms/{room['token']}/tracks", json={"url": video_id(32)}
    )
    assert again.status_code == 201

    waiting = await client.get(f"/r/{room['token']}/live")
    assert 'data-action="requeue"' not in waiting.text
    assert '<span class="badge-soft">queued</span>' in waiting.text

    await _set_state(api, again.json()["id"], STATE_PLAYED)

    # Both history rows are the same song, and it is queueable again from either.
    played = await client.get(f"/r/{room['token']}/live")
    assert played.text.count('data-action="requeue"') == 2


async def test_a_song_back_on_air_says_so_rather_than_offering_itself(api, client, room):
    await _play(api, client, room, 33)
    again = await client.post(
        f"/api/rooms/{room['token']}/tracks", json={"url": video_id(33)}
    )
    await _set_state(api, again.json()["id"], STATE_PLAYING)

    fragment = await client.get(f"/r/{room['token']}/live")

    assert '<span class="badge-soft">on air</span>' in fragment.text
    assert 'data-action="requeue"' not in fragment.text


async def test_the_player_names_the_entry_its_progress_belongs_to(api, client, room):
    """A PLAYBACK_POSITION event says which entry it measured, and the page
    matches it against this before moving the bar -- the last report of a track
    can arrive after the next one is already on screen."""
    track = (
        await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(35)})
    ).json()
    await _set_state(api, track["id"], STATE_PLAYING)

    fragment = await client.get(f"/r/{room['token']}/live")

    assert f'data-entry="{track["id"]}"' in fragment.text


async def test_a_locked_queue_reaches_the_history_button(api, new_client, client, room):
    """The offer is the add box in another shape, so it is closed by the same
    things — with the reason on it rather than a failure after the press."""
    await _play(api, client, room, 34)
    await client.patch(f"/api/rooms/{room['token']}", json={"queue_locked": True})

    guest = await new_client()
    fragment = await guest.get(f"/r/{room['token']}/live")

    assert 'data-action="requeue"' in fragment.text
    assert 'title="The host locked the queue."' in fragment.text


async def test_the_host_key_is_shown_once_and_only_when_handed_over(client, room):
    with_secret = await client.get(f"/r/{room['token']}?secret={room['host_secret']}")
    assert room["host_secret"] in with_secret.text

    plain = await client.get(f"/r/{room['token']}")
    assert room["host_secret"] not in plain.text


async def test_an_unknown_room_renders_a_page_not_json(client):
    response = await client.get("/r/doesnotexistdoesnotexist")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Room not found" in response.text


async def test_a_banned_listener_gets_a_page_too(new_client, client, room):
    guest = await new_client()
    await guest.get(f"/r/{room['token']}")

    listeners = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    victim = next(row for row in listeners if not row["is_host"])
    await client.post(f"/api/rooms/{room['token']}/bans", json={"listener_id": victim["id"]})

    response = await guest.get(f"/r/{room['token']}")
    assert response.status_code == 403
    assert "You can’t join this room" in response.text


async def test_an_unknown_page_renders_the_error_template(client):
    response = await client.get("/no/such/page")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


async def test_api_errors_stay_json(client):
    response = await client.get("/api/rooms/doesnotexistdoesnotexist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


# --- Live fragment ----------------------------------------------------------
async def test_the_live_fragment_carries_every_changing_region(client, room):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(2)})

    response = await client.get(f"/r/{room['token']}/live")

    assert response.status_code == 200
    for region in ("nowplaying", "queue", "history", "meta"):
        assert f'data-region="{region}"' in response.text
    assert 'data-queue-count="1"' in response.text


async def test_the_fragment_never_contains_the_audio_element(client, room):
    """Re-rendering it would tear down playback for everyone listening."""
    response = await client.get(f"/r/{room['token']}/live")
    assert "<audio" not in response.text


async def test_the_queue_lock_reaches_the_fragment(new_client, client, room):
    await client.patch(f"/api/rooms/{room['token']}", json={"queue_locked": True})

    guest = await new_client()
    response = await guest.get(f"/r/{room['token']}/live")

    assert 'data-add-disabled="1"' in response.text
    assert "locked the queue" in response.text


# --- Legal pages ------------------------------------------------------------
async def test_privacy_renders(client):
    response = await client.get("/privacy")

    assert response.status_code == 200
    assert "No audio files." in response.text


async def test_the_imprint_is_absent_until_an_operator_fills_it_in(client):
    response = await client.get("/imprint")

    assert response.status_code == 404
    assert "has not published an Impressum" in response.text
    assert "/imprint" not in (await client.get("/")).text


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
            add_rate_limit=100,
        )

    async def test_the_imprint_page_renders_what_was_configured(self, client):
        response = await client.get("/imprint")

        assert response.status_code == 200
        assert "Example Operator" in response.text
        assert "1010 Vienna" in response.text

    async def test_the_footer_links_the_imprint_and_the_configured_channels(self, client):
        page = await client.get("/")

        assert 'href="/imprint"' in page.text
        assert "https://github.com/crazynudelsieb" in page.text
        assert 'data-user="hello"' in page.text
        assert "hello@example.com" not in page.text

    async def test_the_data_location_reaches_the_privacy_page(self, client):
        assert "Austria" in (await client.get("/privacy")).text
