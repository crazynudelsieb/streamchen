"""Room lifecycle and the room snapshot the frontend renders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.api.deps import (
    current_listener,
    get_db,
    get_redis,
    get_session_id,
    get_settings_dep,
    has_host_secret,
    require_host,
    require_room,
)
from app.api.playback import playback_position
from app.config import Settings
from app.models import Listener, Room
from app.schemas import (
    ListenerRow,
    RoomCreate,
    RoomCreated,
    RoomSettingsUpdate,
    RoomState,
)
from app.service import (
    RADIO_SESSION_ID,
    create_room,
    current_track,
    join_room,
    listener_info,
    now_playing,
    pending_count_for,
    queued_tracks,
    recent_tracks,
    room_settings,
    serialize_track,
    stream_url,
)

router = APIRouter(tags=["rooms"])


@router.post("/rooms", response_model=RoomCreated, status_code=status.HTTP_201_CREATED)
async def create(
    payload: RoomCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    session_id: str = Depends(get_session_id),
) -> RoomCreated:
    """Create a room and become its host.

    The host secret is in this response and nowhere else, ever again.
    """
    room, host_secret = await create_room(db, settings, payload.name)
    await join_room(db, room, session_id, as_host=True)
    return RoomCreated(
        token=room.token,
        name=room.name,
        url=f"{settings.base_url}/r/{room.token}",
        host_secret=host_secret,
    )


async def build_state(
    request: Request,
    room: Room,
    listener: Listener,
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> RoomState:
    """The whole page in one object — the frontend stores none of it."""
    playing = await current_track(db, room.id)
    position = await playback_position(redis, room.id, playing.id if playing else None)

    queue = await queued_tracks(db, room.id, include_shadow_for=listener.id)
    history = await recent_tracks(db, room.id)

    online = await events.count_present(redis, room.id)
    if online == 0:
        # Presence has a TTL; the viewer holding this request is at least here.
        online = 1

    return RoomState(
        token=room.token,
        name=room.name,
        settings=room_settings(room),
        stream_url=stream_url(settings, room),
        me=listener_info(listener),
        is_host=listener.is_host or has_host_secret(request, room),
        listeners=online,
        now_playing=now_playing(playing, listener, position),
        queue=[serialize_track(track, listener) for track in queue],
        history=[serialize_track(track, listener) for track in history],
    )


@router.get("/rooms/{token}", response_model=RoomState)
async def read(
    request: Request,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> RoomState:
    if await events.mark_present(redis, room.id, listener.session_id):
        await events.publish(
            redis, room.id, events.LISTENER_JOINED, {"display_name": listener.display_name}
        )
    return await build_state(request, room, listener, db, redis, settings)


@router.patch("/rooms/{token}", response_model=RoomState)
async def update(
    request: Request,
    payload: RoomSettingsUpdate,
    room: Room = Depends(require_host),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> RoomState:
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] is not None:
        name = changes["name"].strip()[: settings.room_name_max_length]
        if name:
            room.name = name
    for field in (
        "voting_enabled",
        "queue_locked",
        "max_pending_per_listener",
        "max_listeners",
    ):
        if changes.get(field) is not None:
            setattr(room, field, changes[field])
    if "fallback_playlist" in changes:
        room.fallback_playlist = (changes["fallback_playlist"] or "").strip() or None

    await db.flush()
    await events.publish(redis, room.id, events.ROOM_UPDATED, {"name": room.name})
    return await build_state(request, room, listener, db, redis, settings)


@router.delete("/rooms/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def destroy(
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    room_id = room.id
    await db.delete(room)
    await events.set_now_playing(redis, room_id, None)
    await events.publish(redis, room_id, events.ROOM_UPDATED, {"deleted": True})


@router.get("/rooms/{token}/listeners", response_model=list[ListenerRow])
async def listeners(
    room: Room = Depends(require_host),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> list[ListenerRow]:
    """Host view: who is in the room and how much of the queue is theirs."""
    result = await db.execute(
        select(Listener)
        .where(Listener.room_id == room.id, Listener.session_id != RADIO_SESSION_ID)
        .order_by(Listener.created_at)
    )
    rows: list[ListenerRow] = []
    for listener in result.scalars().all():
        online = await redis.exists(events.presence_key(room.id, listener.session_id))
        rows.append(
            ListenerRow(
                id=listener.id,
                display_name=listener.display_name,
                is_host=listener.is_host,
                online=bool(online),
                queued=await pending_count_for(db, room.id, listener.id),
                shadow_banned=listener.is_shadow_banned(),
            )
        )
    return rows
