"""Generated cat avatars.

The point of drawing them here is that the browser asks nobody else who is in
the room, so most of what matters is negative: no third party, no per-request
work, and no way to make the endpoint render something it was not asked for.
"""

from __future__ import annotations

import uuid
from xml.etree import ElementTree

import pytest

from app.avatars import avatar_seed, cat_svg


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
