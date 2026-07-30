"""Generated cat avatars.

The point of drawing them here is that the browser asks nobody else who is in
the room, so most of what matters is negative: no third party, no per-request
work, and no way to make the endpoint render something it was not asked for.
"""

from __future__ import annotations

import uuid
from xml.etree import ElementTree

import pytest

from app.avatars import avatar_seed, cat_svg, listener_seed, new_seed
from app.models import Listener
from tests.conftest import watch_url


def test_a_listener_is_always_the_same_cat():
    """An avatar that changed between page loads would be noise, not identity."""
    listener = uuid.uuid4()

    assert avatar_seed(listener) == avatar_seed(listener)
    assert cat_svg(avatar_seed(listener)) == cat_svg(avatar_seed(listener))


def test_different_listeners_get_different_cats():
    seeds = {avatar_seed(uuid.uuid4()) for _ in range(200)}

    assert len(seeds) == 200


def test_cats_actually_differ_and_not_just_their_seeds():
    drawings = {cat_svg(avatar_seed(uuid.uuid4())) for _ in range(60)}

    # 8 furs x 5 eyes x 6 backgrounds x 4 markings x tilt x ears x pupils.
    assert len(drawings) > 40


def test_the_seed_does_not_carry_the_listener_id_around():
    listener = uuid.uuid4()

    assert str(listener) not in avatar_seed(listener)
    assert str(listener).replace("-", "") not in avatar_seed(listener)


def test_a_cat_is_a_valid_svg_document():
    root = ElementTree.fromstring(cat_svg(avatar_seed("anybody")))

    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 64 64"


def test_a_cat_is_small_enough_to_be_free():
    assert len(cat_svg(avatar_seed("anybody"))) < 2048


def test_any_string_can_be_a_seed():
    """Seeds come from ids, and one day something else. Nothing may explode."""
    for value in ("", "radio", "a" * 200, "🐈", str(uuid.uuid4())):
        assert cat_svg(avatar_seed(value)).startswith("<svg")


# --- Asking for another cat -------------------------------------------------
def test_a_listener_without_a_chosen_cat_is_the_one_their_id_hashes_to():
    listener = Listener(id=uuid.uuid4(), session_id="s", display_name="anna")

    assert listener_seed(listener) == avatar_seed(listener.id)


def test_a_chosen_cat_is_the_one_drawn_instead():
    listener = Listener(id=uuid.uuid4(), session_id="s", display_name="anna")
    listener.chosen_avatar = new_seed()

    assert listener_seed(listener) == listener.chosen_avatar
    assert listener_seed(listener) != avatar_seed(listener.id)


def test_a_new_seed_is_one_the_endpoint_will_serve():
    """Same shape as a hashed one, so nothing downstream has to know which it is."""
    seed = new_seed()

    assert len(seed) == len(avatar_seed(uuid.uuid4()))
    assert int(seed, 16) >= 0


def test_two_new_seeds_are_not_the_same_cat_twice():
    seeds = {new_seed() for _ in range(200)}

    assert len(seeds) == 200


def test_a_new_seed_draws_a_different_cat_from_the_one_being_replaced():
    """A re-roll that lands on the same drawing looks like a button that did
    nothing, so it is asked to avoid the cat the listener is looking at."""
    for _ in range(60):
        current = avatar_seed(uuid.uuid4())

        assert cat_svg(new_seed(unlike=current)) != cat_svg(current)


# --- The endpoint -----------------------------------------------------------
async def test_the_avatar_endpoint_serves_an_svg(client):
    response = await client.get(f"/a/{avatar_seed(uuid.uuid4())}.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")


async def test_an_avatar_is_cached_forever(client):
    """It is generated from its own URL: it can never be stale."""
    response = await client.get(f"/a/{avatar_seed(uuid.uuid4())}.svg")

    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize(
    "seed",
    ["../../etc/passwd", "<script>", "ZZZZZZZZ", "abc", "a" * 40],
)
async def test_the_endpoint_only_answers_to_a_seed(client, seed):
    response = await client.get(f"/a/{seed}.svg")

    assert response.status_code == 404


# --- Asking for another cat, over HTTP ---------------------------------------
async def test_a_listener_can_ask_to_be_drawn_as_a_different_cat(client, room):
    before = (await client.get(f"/api/rooms/{room['token']}")).json()["me"]["avatar"]

    response = await client.post(f"/api/rooms/{room['token']}/me/avatar")

    assert response.status_code == 200
    chosen = response.json()["avatar"]
    assert chosen != before
    # And it is drawable, which is the only thing a seed has to be.
    assert (await client.get(f"/a/{chosen}.svg")).status_code == 200


async def test_the_new_cat_is_the_one_they_keep(client, room):
    """An avatar that only lasted until the next page load would be noise."""
    chosen = (await client.post(f"/api/rooms/{room['token']}/me/avatar")).json()["avatar"]

    state = await client.get(f"/api/rooms/{room['token']}")

    assert state.json()["me"]["avatar"] == chosen


async def test_a_new_cat_leaves_their_name_alone(client, room):
    await client.patch(f"/api/rooms/{room['token']}/me", json={"display_name": "DJ Anna"})

    response = await client.post(f"/api/rooms/{room['token']}/me/avatar")

    assert response.json()["display_name"] == "DJ Anna"


async def test_the_new_cat_is_what_the_room_sees_on_their_tracks(client, room):
    await client.post(f"/api/rooms/{room['token']}/tracks", json={"url": watch_url(1)})
    chosen = (await client.post(f"/api/rooms/{room['token']}/me/avatar")).json()["avatar"]

    state = (await client.get(f"/api/rooms/{room['token']}")).json()

    assert state["queue"][0]["added_by_avatar"] == chosen


async def test_the_new_cat_is_who_the_roster_shows(client, room):
    chosen = (await client.post(f"/api/rooms/{room['token']}/me/avatar")).json()["avatar"]

    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert [row["avatar"] for row in rows if row["is_me"]] == [chosen]


async def test_a_new_cat_grants_nothing(new_client, room):
    guest = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")

    response = await guest.post(f"/api/rooms/{room['token']}/me/avatar")

    assert response.status_code == 200
    assert response.json()["is_host"] is False


async def test_one_listener_rerolling_does_not_touch_another(new_client, room, client):
    guest = await new_client()
    before = (await guest.get(f"/api/rooms/{room['token']}")).json()["me"]["avatar"]

    await client.post(f"/api/rooms/{room['token']}/me/avatar")

    assert (await guest.get(f"/api/rooms/{room['token']}")).json()["me"]["avatar"] == before


async def test_rerolling_is_rate_limited(client, room, api):
    api.state.settings = api.state.settings.__class__(
        **(api.state.settings.__dict__ | {"rename_rate_limit": 2, "rename_rate_window_s": 60})
    )

    codes = [
        (await client.post(f"/api/rooms/{room['token']}/me/avatar")).status_code
        for _ in range(4)
    ]

    assert codes[:2] == [200, 200]
    assert 429 in codes[2:]


async def test_rerolling_does_not_spend_the_rename_allowance(client, room, api):
    """Shuffling cats must not be why somebody cannot rename themselves."""
    api.state.settings = api.state.settings.__class__(
        **(api.state.settings.__dict__ | {"rename_rate_limit": 2, "rename_rate_window_s": 60})
    )

    for _ in range(2):
        await client.post(f"/api/rooms/{room['token']}/me/avatar")

    response = await client.patch(
        f"/api/rooms/{room['token']}/me", json={"display_name": "DJ Anna"}
    )

    assert response.status_code == 200
