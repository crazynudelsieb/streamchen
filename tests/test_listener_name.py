"""Choosing your own name in a room, without it becoming an account."""

from __future__ import annotations

from app.service import clean_display_name, generate_display_name
from tests.conftest import watch_url


# --- Cleaning ---------------------------------------------------------------
def test_a_chosen_name_survives_intact():
    assert clean_display_name("Anna") == "Anna"


def test_surrounding_and_repeated_whitespace_is_collapsed():
    assert clean_display_name("  the   dancer  ") == "the dancer"


def test_a_pasted_line_break_does_not_become_a_two_line_name():
    assert clean_display_name("first\nsecond") == "first second"


def test_unprintable_characters_are_dropped():
    assert clean_display_name("bo\u200bb\x00") == "bob"


def test_a_long_name_is_truncated_to_what_the_column_holds():
    assert len(clean_display_name("n" * 200)) == 40


def test_a_name_that_is_only_whitespace_counts_as_no_name():
    assert clean_display_name("   \t ") == ""
    assert clean_display_name(None) == ""


# --- The endpoint -----------------------------------------------------------
async def test_a_listener_can_rename_themselves(client, room):
    response = await client.patch(
        f"/api/rooms/{room['token']}/me", json={"display_name": "DJ Anna"}
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "DJ Anna"

    state = await client.get(f"/api/rooms/{room['token']}")
    assert state.json()["me"]["display_name"] == "DJ Anna"


async def test_the_new_name_is_what_the_room_sees_on_their_tracks(client, room):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})
    await client.patch(f"/api/rooms/{room['token']}/me", json={"display_name": "DJ Anna"})

    state = await client.get(f"/api/rooms/{room['token']}")
    assert state.json()["queue"][0]["added_by"] == "DJ Anna"


async def test_a_blank_name_asks_for_another_generated_one(client, room):
    before = (await client.get(f"/api/rooms/{room['token']}")).json()["me"]["display_name"]

    response = await client.patch(f"/api/rooms/{room['token']}/me", json={"display_name": "  "})

    assert response.status_code == 200
    chosen = response.json()["display_name"]
    assert chosen
    # A generated name, not the blank that was sent -- the autonaming stays
    # reachable after a rename rather than the first edit being final.
    assert "-" in chosen
    assert chosen != "  "
    assert before  # and the listener was never left nameless


async def test_renaming_does_not_grant_host_rights(new_client, room):
    guest = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")

    response = await guest.patch(
        f"/api/rooms/{room['token']}/me", json={"display_name": "the host"}
    )

    assert response.status_code == 200
    assert response.json()["is_host"] is False
    assert (await guest.get(f"/api/rooms/{room['token']}")).json()["is_host"] is False


async def test_a_name_longer_than_the_field_is_refused_rather_than_silently_cut(client, room):
    response = await client.patch(
        f"/api/rooms/{room['token']}/me", json={"display_name": "n" * 41}
    )
    assert response.status_code == 422


async def test_renaming_is_rate_limited(client, room, api):
    api.state.settings = api.state.settings.__class__(
        **(api.state.settings.__dict__ | {"rename_rate_limit": 2, "rename_rate_window_s": 60})
    )

    codes = []
    for index in range(4):
        response = await client.patch(
            f"/api/rooms/{room['token']}/me", json={"display_name": f"name {index}"}
        )
        codes.append(response.status_code)

    assert codes[:2] == [200, 200]
    assert 429 in codes[2:]


async def test_one_listener_renaming_does_not_touch_another(new_client, room, client):
    guest = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")
    await guest.patch(f"/api/rooms/{room['token']}/me", json={"display_name": "guest"})

    mine = (await client.get(f"/api/rooms/{room['token']}")).json()["me"]["display_name"]
    assert mine != "guest"


def test_generated_names_are_still_the_default_shape():
    assert "-" in generate_display_name()
