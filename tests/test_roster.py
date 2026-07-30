"""Who is in the room, and who only looks like they are.

The count in the header and the list behind it are one answer (concept §14):
presence in Redis is a hint that expires and can outlive the listener it was
written for, so anything shown to people is that hint *and* a row in Postgres.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from app import events
from app.service import RADIO_SESSION_ID, get_room, radio_listener


async def room_id(api, token: str) -> uuid.UUID:
    async with api.state.sessionmaker() as db:
        room = await get_room(db, token)
        return room.id


def names(rows: list[dict]) -> set[str]:
    return {row["display_name"] for row in rows}


async def test_the_roster_is_everybody_in_the_room(new_client, client, room):
    guest = await new_client()
    guest_state = (await guest.get(f"/api/rooms/{room['token']}")).json()
    host_state = (await client.get(f"/api/rooms/{room['token']}")).json()

    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert names(rows) == {host_state["me"]["display_name"], guest_state["me"]["display_name"]}
    assert [row["is_host"] for row in rows].count(True) == 1
    # The same list, read by two people, points at a different "you" each time.
    mine = [row for row in rows if row["is_me"]]
    assert len(mine) == 1 and mine[0]["id"] == host_state["me"]["id"]

    theirs = (await guest.get(f"/api/rooms/{room['token']}/roster")).json()
    assert [row for row in theirs if row["is_me"]][0]["id"] == guest_state["me"]["id"]


async def test_the_roster_says_nothing_about_moderation(client, room):
    """A shadow-banned listener must not be able to learn that they are, so the
    roster carries none of it -- that is the host's list, not this one."""
    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert rows
    assert set(rows[0]) == {"id", "display_name", "avatar", "is_host", "is_me"}


async def test_you_are_in_your_own_room_before_presence_is_written(client, room):
    """The very first snapshot is taken between the join and the presence key."""
    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert state["listeners"] == 1
    assert [row["id"] for row in rows] == [state["me"]["id"]]


async def test_a_stranger_is_not_a_listener(api, client, room):
    """The bug this guards: a presence key with no listener behind it inflated
    the header count while the list it opens stayed right."""
    await events.mark_present(api.state.redis, await room_id(api, room["token"]), str(uuid.uuid4()))

    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert state["listeners"] == 1
    assert len(rows) == 1


async def test_a_removed_listener_stops_counting(api, new_client, client, room):
    """Their socket may still be open, and it keeps refreshing presence until it
    notices; the room they are no longer in must not count them meanwhile."""
    guest: AsyncClient = await new_client()
    await guest.get(f"/api/rooms/{room['token']}")
    guest_session = guest.cookies["sc_session"]

    rows = (await client.get(f"/api/rooms/{room['token']}/listeners")).json()
    victim = next(row for row in rows if not row["is_host"])
    banned = await client.post(
        f"/api/rooms/{room['token']}/bans", json={"listener_id": victim["id"]}
    )
    assert banned.status_code == 201

    # The heartbeat of a socket that has not hung up yet.
    await events.mark_present(api.state.redis, await room_id(api, room["token"]), guest_session)

    state = (await client.get(f"/api/rooms/{room['token']}")).json()
    assert state["listeners"] == 1
    assert len((await client.get(f"/api/rooms/{room['token']}/roster")).json()) == 1


async def test_the_radio_is_not_in_the_roster(api, client, room):
    """Autoplay's stand-in submitter is not a participant, even if something has
    written presence for it."""
    identifier = await room_id(api, room["token"])
    async with api.state.sessionmaker() as db:
        await radio_listener(db, await get_room(db, room["token"]))
        await db.commit()
    await events.mark_present(api.state.redis, identifier, RADIO_SESSION_ID)

    rows = (await client.get(f"/api/rooms/{room['token']}/roster")).json()

    assert "radio" not in names(rows)
    assert (await client.get(f"/api/rooms/{room['token']}")).json()["listeners"] == 1
