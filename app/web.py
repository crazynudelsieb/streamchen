"""Server-rendered pages.

The frontend owns no state (concept §3), which is exactly what a rendered page
is: the server decides what the room looks like and sends the markup. The JSON
API underneath is unchanged — ``app.js`` calls it for actions and then swaps in
a freshly rendered fragment, so there is one implementation of "what the queue
looks like" rather than two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__, events
from app.api.deps import (
    HOST_SECRET_HEADER,
    current_listener,
    get_db,
    get_redis,
    get_settings_dep,
    has_host_secret,
    require_room,
)
from app.api.playback import playback_position
from app.config import Settings
from app.contact import imprint_payload, legal_payload
from app.models import Listener, Room
from app.service import (
    current_track,
    now_playing,
    queued_tracks,
    recent_tracks,
    serialize_track,
    stream_url,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(include_in_schema=False)

FEATURES = (
    {
        "icon": "bi-link-45deg",
        "title": "One link, no accounts",
        "body": "Create a room, send the link. Anyone who opens it is in — "
        "nothing to sign up for.",
    },
    {
        "icon": "bi-people",
        "title": "Everyone queues",
        "body": "Round-robin scheduling gives every listener a turn, so nobody "
        "can take over the queue.",
    },
    {
        "icon": "bi-hand-thumbs-up",
        "title": "The room decides",
        "body": "Up- and downvotes reorder what plays next, within each "
        "person’s turn.",
    },
    {
        "icon": "bi-shield-check",
        "title": "Nothing is kept",
        "body": "Audio is streamed and discarded. No media survives playback, "
        "by design.",
    },
)


# --- Filters ----------------------------------------------------------------
def format_duration(seconds: float | int | None) -> str:
    """``3:07``, or ``1:02:33`` once a track passes an hour."""
    total = int(seconds or 0)
    if total <= 0:
        return "0:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_score(score: int) -> str:
    """The sign matters more than the number."""
    return f"+{score}" if score > 0 else str(score)


def format_ago(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def build_templates(settings: Settings) -> Jinja2Templates:
    """One environment, with everything process-wide baked in as a global.

    Contact details, the app name and the imprint switch are the same for every
    request on an instance, so they belong here rather than in each handler's
    context (and every page extends base.html, which renders the footer).
    """
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    legal = legal_payload(settings)

    templates.env.filters["duration"] = format_duration
    templates.env.filters["score"] = format_score
    templates.env.filters["ago"] = format_ago

    templates.env.globals.update(
        APP_NAME=settings.app_name,
        VERSION=__version__,
        SEO_ENABLED=settings.seo_enabled,
        SEO_SITE_NAME=settings.seo_site_name,
        SEO_DESCRIPTION=settings.seo_description,
        contact_links=legal["contact_links"],
        contact_email=legal["contact_email"],
        license_email=legal["license_email"],
        imprint_enabled=legal["imprint_enabled"],
        data_location=settings.data_location,
        room_idle_days=settings.room_idle_days,
        # Static URLs carry the release so the immutable cache header is safe.
        static=lambda path: f"/static/{path}?v={__version__}",
        no_index=False,
        show_connection=False,
    )
    return templates


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _page(
    request: Request,
    name: str,
    context: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request=request, name=name, context=context or {}, status_code=status_code
    )


def error_page(
    request: Request,
    *,
    status_code: int,
    heading: str,
    message: str,
    icon: str = "bi-signpost-split",
    action: str = "Back to the start",
) -> HTMLResponse:
    return _page(
        request,
        "error.html",
        {"heading": heading, "message": message, "icon": icon, "action": action},
        status_code=status_code,
    )


# --- Pages ------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(request, "index.html", {"features": FEATURES, "no_index": False})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return _page(request, "privacy.html")


@router.get("/imprint", response_class=HTMLResponse)
async def imprint(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    if not settings.imprint_enabled:
        return error_page(
            request,
            status_code=404,
            heading="No site notice",
            message="This instance has not published an Impressum.",
        )
    return _page(request, "imprint.html", {"imprint": imprint_payload(settings)})


async def _room_context(
    request: Request,
    room: Room,
    listener: Listener,
    db: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict:
    """Everything both the full page and its fragments render from."""
    is_host = listener.is_host or has_host_secret(request, room)

    playing = await current_track(db, room.id)
    position = await playback_position(redis, room.id, playing.id if playing else None)
    state = now_playing(playing, listener, position)

    queue = [
        serialize_track(track, listener)
        for track in await queued_tracks(db, room.id, include_shadow_for=listener.id)
    ]
    history = [serialize_track(track, listener) for track in await recent_tracks(db, room.id)]

    online = await events.count_present(redis, room.id) or 1

    pending = sum(1 for track in queue if track.mine)
    if room.queue_locked and not is_host:
        add_disabled_reason = "The host locked the queue."
    elif not is_host and pending >= room.max_pending_per_listener:
        add_disabled_reason = f"You already have {pending} song(s) waiting."
    else:
        add_disabled_reason = ""

    duration = state.track.duration_s if state.track else 0
    progress = round(min(100.0, position / duration * 100), 1) if duration else 0.0

    return {
        "room": room,
        "me": listener,
        "is_host": is_host,
        "listeners": online,
        "now_playing": state,
        "progress_percent": progress,
        "queue": queue,
        "history": history,
        "voting_enabled": room.voting_enabled,
        "add_disabled_reason": add_disabled_reason,
        "stream_url": stream_url(settings, room),
        "share_url": f"{settings.base_url}/r/{room.token}",
        # Rooms are private by design: never index one.
        "no_index": True,
        "show_connection": True,
    }


@router.get("/r/{token}", response_class=HTMLResponse)
async def room_page(
    request: Request,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    if await events.mark_present(redis, room.id, listener.session_id):
        await events.publish(
            redis, room.id, events.LISTENER_JOINED, {"display_name": listener.display_name}
        )
        # Somebody just opened the room, so it is about to want a source. The
        # presence TTL is what keeps this from firing on every poll.
        await events.request_worker(redis, room.id)

    context = await _room_context(request, room, listener, db, redis, settings)
    # Handed over exactly once, immediately after creation, by the redirect.
    context["host_secret"] = request.query_params.get("secret", "")
    return _page(request, "room.html", context)


@router.get("/r/{token}/live", response_class=HTMLResponse)
async def room_fragment(
    request: Request,
    room: Room = Depends(require_room),
    listener: Listener = Depends(current_listener),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    """The parts of the room that change, in one response.

    Swapping player, queue and history together means they can never disagree
    with each other the way three independent polls could.
    """
    context = await _room_context(request, room, listener, db, redis, settings)
    return _page(request, "_live.html", context)


__all__ = [
    "HOST_SECRET_HEADER",
    "STATIC_DIR",
    "build_templates",
    "error_page",
    "router",
]
