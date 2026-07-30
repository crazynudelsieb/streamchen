"""WebSocket gateway.

Thin by design: the socket carries *notifications*, not data. A client that
receives one refetches the room snapshot over HTTP, which means a dropped
connection or a flushed Redis can never leave the page showing something the
database disagrees with.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app import events
from app.models import Room
from app.security import is_valid_room_token, new_session_id
from app.service import get_room, is_banned, join_room

logger = logging.getLogger(__name__)
router = APIRouter()

PING_INTERVAL_S = 25

# Close codes in the private range, for the two answers that are not "here is
# your socket". The client stops reconnecting when it hears them.
CLOSE_NO_ROOM = 4404
CLOSE_REMOVED = 4403


async def _presence_heartbeat(redis: Redis, room: Room, session_id: str) -> None:
    """Presence keys expire; hold ours open for as long as the socket is."""
    while True:
        await asyncio.sleep(events.PRESENCE_TTL_S // 2)
        await events.mark_present(redis, room.id, session_id)


def _removes(data: str, listener_id: str) -> bool:
    """Is this event the host removing the listener on the other end?

    Being told is what makes a kick stick. The socket outlives the row it was
    opened for -- and while it is open its heartbeat keeps writing presence, so
    a listener nobody can see would still be counted as being in the room.
    """
    try:
        event = json.loads(data)
    except ValueError:  # pragma: no cover - we publish the JSON ourselves
        return False
    payload = event.get("data") or {}
    return (
        event.get("type") == events.LISTENER_LEFT
        and bool(payload.get("banned"))
        and payload.get("listener_id") == listener_id
    )


async def _relay(websocket: WebSocket, pubsub, listener_id: str) -> None:
    async for message in pubsub.listen():
        if message is None or message.get("type") != "message":
            continue
        data = message["data"]
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if _removes(data, listener_id):
            # Hanging up wakes the handler, which drops presence on its way out.
            await websocket.close(code=CLOSE_REMOVED)
            return
        await websocket.send_text(data)


@router.websocket("/rooms/{token}/ws")
async def room_socket(websocket: WebSocket, token: str) -> None:
    settings = websocket.app.state.settings
    redis: Redis = websocket.app.state.redis
    sessionmaker = websocket.app.state.sessionmaker

    if not is_valid_room_token(token):
        await websocket.close(code=CLOSE_NO_ROOM)
        return

    session_id = websocket.cookies.get(settings.session_cookie) or new_session_id()

    async with sessionmaker() as db:
        room = await get_room(db, token)
        if room is None:
            await websocket.close(code=CLOSE_NO_ROOM)
            return
        # Before the join, which would otherwise hand a removed listener a new
        # row and undo the kick they were refused the room over.
        if await is_banned(db, room.id, session_id):
            await websocket.close(code=CLOSE_REMOVED)
            return
        listener = await join_room(db, room, session_id)
        display_name = listener.display_name
        listener_id = str(listener.id)
        room_id = room.id
        await db.commit()

    await websocket.accept()

    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(events.channel(room_id))

    if await events.mark_present(redis, room_id, session_id):
        await events.publish(
            redis, room_id, events.LISTENER_JOINED, {"display_name": display_name}
        )

    await websocket.send_text(json.dumps({"type": "READY", "data": {"room": token}}))

    relay = asyncio.create_task(_relay(websocket, pubsub, listener_id))
    heartbeat = asyncio.create_task(_presence_heartbeat(redis, room, session_id))

    try:
        while True:
            # The client only ever pings. Reading is how we notice it left.
            raw = await websocket.receive_text()
            if raw == "ping":
                await websocket.send_text(json.dumps({"type": "PONG", "data": {}}))
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport noise
        logger.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        for task in (relay, heartbeat):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(events.channel(room_id))
            await pubsub.aclose()
        await events.mark_absent(redis, room_id, session_id)
        await events.publish(
            redis, room_id, events.LISTENER_LEFT, {"display_name": display_name}
        )
