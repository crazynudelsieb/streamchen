"""Room lifecycle and the room snapshot the frontend renders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from app.api.playback import room_view
from app.avatars import listener_seed
from app.config import Settings
from app.models import Listener, Room, utcnow
from app.ratelimit import Limit, check
from app.schemas import (
    ListenerInfo,
    ListenerRename,
    ListenerRow,
    RoomCreate,
    RoomCreated,
    RoomSettingsUpdate,
    RoomState,
    RosterRow,
)
from app.service import (
    RADIO_SESSION_ID,
    create_room,
    join_room,
    listener_info,
    online_listeners,
    pending_counts,
    rename_listener,
    reroll_avatar,
    room_settings,
    stream_url,
)
from app.youtube import is_seedless_mix

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
    # Deliberately no worker request: a new room is created with its stream
    # stopped, so there is nothing for a worker to connect to yet. Pressing
    # "Start stream" is what asks for one, and that is one PATCH away.
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
    view = await room_view(db, redis, room, listener)

    return RoomState(
        token=room.token,
        name=room.name,
        settings=room_settings(room),
        stream_url=stream_url(settings, room),
        me=listener_info(listener),
        is_host=listener.is_host or has_host_secret(request, room),
        listeners=view.listeners,
        now_playing=view.now_playing,
        queue=view.queue,
        history=view.history,
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
        # Renaming touches the name and nothing else. The token is the link and
        # is never derived from the name, so a room can be renamed as often as
        # its host likes without breaking anybody's bookmark.
        name = changes["name"].strip()[: settings.room_name_max_length]
        if name:
            room.name = name
    for field in (
        "voting_enabled",
        "queue_locked",
        "stream_stopped",
        "chat_enabled",
        "max_pending_per_listener",
        "max_listeners",
        "news_enabled",
        "news_interval_min",
    ):
        if changes.get(field) is not None:
            setattr(room, field, changes[field])
    if "fallback_playlist" in changes:
        field = (changes["fallback_playlist"] or "").strip()
        # A mix link copied from a page showing no video has lost its seed, and
        # nothing can resolve it. Saying so beats storing it and leaving a host
        # to wonder why the radio never plays any of it.
        seedless = [chunk for chunk in field.replace(",", " ").split() if is_seedless_mix(chunk)]
        if seedless:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "That mix link has no song in it. Open the mix, start a track, "
                "and copy the link from there — a mix is only playable from "
                "somewhere inside it.",
            )
        room.fallback_playlist = field or None
        if room.fallback_playlist:
            # Pasting a playlist into a quiet room is a request for music, so
            # the room counts as active from here. Without this a room whose
            # last listener registered a while ago is outside the window the
            # sweep looks at, and the host's list sits there playing nothing.
            room.last_active_at = utcnow()

    await db.flush()

    if changes.get("stream_stopped") is not None or (
        "fallback_playlist" in changes and room.fallback_playlist
    ):
        # Both directions: a sweep decides who gets a source and who loses one,
        # and it would get here within a few seconds by itself. Those are the
        # seconds a host spends looking at the player wondering whether the
        # button did anything.
        #
        # A saved radio playlist is the same wait wearing different clothes:
        # the room has something to play now and no source to play it on.
        await events.request_worker(redis, room.id)

    await events.publish(
        redis,
        room.id,
        events.ROOM_UPDATED,
        {"name": room.name, "stream_stopped": room.stream_stopped},
    )
    return await build_state(request, room, listener, db, redis, settings)


@router.patch("/rooms/{token}/me", response_model=ListenerInfo)
async def rename_me(
    payload: ListenerRename,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> ListenerInfo:
    """Choose your own name, or send nothing to be given another generated one.

    A name is assigned on join so that nobody has to pick one to take part; this
    only lets somebody who wants to be recognisable say so. It grants nothing —
    host rights come from the key, and a radio pick is identified by its
    session, so neither can be impersonated by taking their name.
    """
    verdict = await check(
        redis,
        "rename",
        str(listener.id),
        Limit(settings.rename_rate_limit, settings.rename_rate_window_s),
    )
    if not verdict.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Slow down a moment.",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )

    await rename_listener(db, listener, payload.display_name)
    # Their name is on every track they queued and on the history, so every
    # client's view of the room is now stale.
    await events.publish(redis, room.id, events.QUEUE_CHANGED, {})
    return listener_info(listener)


@router.post("/rooms/{token}/me/avatar", response_model=ListenerInfo)
async def new_avatar(
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> ListenerInfo:
    """Be drawn as a different cat.

    The avatar a listener starts with comes from their id, so it is the one thing
    about themselves they cannot change by picking a name — hence a button. Like
    the name it grants nothing and identifies nobody: the seed is random, lives
    on the listener row for this room, and is not derived from anything.

    Its own rate-limit bucket, on the name's allowance: shuffling cats should not
    be the reason somebody cannot rename themselves.
    """
    verdict = await check(
        redis,
        "avatar",
        str(listener.id),
        Limit(settings.rename_rate_limit, settings.rename_rate_window_s),
    )
    if not verdict.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Slow down a moment.",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )

    await reroll_avatar(db, listener)
    # Their cat is beside their name on every track they queued and in the
    # history, so the same views go stale as after a rename.
    await events.publish(redis, room.id, events.QUEUE_CHANGED, {})
    return listener_info(listener)


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


@router.get("/rooms/{token}/roster", response_model=list[RosterRow])
async def roster(
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> list[RosterRow]:
    """Who is listening, for anybody in the room.

    The listener count in the header opens this, so it lists exactly the people
    that count -- present, and never the radio. Unlike the host's list it says
    nothing about queues or moderation: this is "who is here", not a panel.
    """
    rows = await online_listeners(db, redis, room.id)
    if all(row.id != listener.id for row in rows):
        # Presence is written just after the join; between the two the viewer
        # would otherwise be missing from their own room.
        rows.insert(0, listener)

    return [
        RosterRow(
            id=row.id,
            display_name=row.display_name,
            avatar=listener_seed(row),
            is_host=row.is_host,
            is_me=row.id == listener.id,
        )
        for row in rows
    ]


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
    # Both asked once for the room rather than once per listener: this list is
    # every session that ever opened the link, and a host with a busy room
    # should not pay a round trip per name in it.
    present = await events.present_sessions(redis, room.id)
    queued = await pending_counts(db, room.id)

    return [
        ListenerRow(
            id=listener.id,
            display_name=listener.display_name,
            avatar=listener_seed(listener),
            is_host=listener.is_host,
            online=listener.session_id in present,
            queued=queued.get(listener.id, 0),
            shadow_banned=listener.is_shadow_banned(),
        )
        for listener in result.scalars().all()
    ]
