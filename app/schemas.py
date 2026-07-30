"""Request and response shapes.

The frontend owns no state (concept §3), so these payloads are the entire
contract: whatever a client needs to render, it is in here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# --- Rooms ------------------------------------------------------------------
class RoomCreate(BaseModel):
    name: str = Field(default="", max_length=80)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        return value.strip()


class RoomSettingsUpdate(BaseModel):
    """Every field optional: a host PATCHes only what they are changing."""

    name: str | None = Field(default=None, max_length=80)
    voting_enabled: bool | None = None
    queue_locked: bool | None = None
    stream_stopped: bool | None = None
    chat_enabled: bool | None = None
    max_pending_per_listener: int | None = Field(default=None, ge=1, le=50)
    max_listeners: int | None = Field(default=None, ge=1, le=10_000)
    fallback_playlist: str | None = Field(default=None, max_length=4000)


class RoomSettings(BaseModel):
    voting_enabled: bool
    queue_locked: bool
    # The host stopped the stream. The room, its queue and its link are all
    # still here; only the audio source is gone.
    stream_stopped: bool = False
    chat_enabled: bool = True
    max_pending_per_listener: int
    max_listeners: int
    fallback_playlist: str | None = None


class RoomCreated(BaseModel):
    token: str
    name: str
    url: str
    # Shown once, then it is the host's browser's problem. The server keeps
    # only an Argon2 hash of it.
    host_secret: str


class ListenerInfo(BaseModel):
    id: uuid.UUID
    display_name: str
    is_host: bool


class ListenerRename(BaseModel):
    """What a listener wants to be called in this room.

    Blank is meaningful: it asks for another generated name rather than for no
    name at all, so the naming system stays reachable after a rename.
    """

    display_name: str = Field(default="", max_length=40)


class TrackOut(BaseModel):
    id: uuid.UUID
    youtube_id: str
    title: str
    duration_s: int
    thumbnail_url: str | None = None
    channel: str | None = None
    state: str
    score: int
    upvotes: int
    downvotes: int
    added_by: str
    added_by_id: uuid.UUID
    mine: bool = False
    my_vote: int = 0
    # Queued by autoplay rather than by a listener.
    radio: bool = False
    # Only ever true on the submitter's own view of their own track.
    shadowed: bool = False
    created_at: datetime
    started_at: datetime | None = None


class NowPlaying(BaseModel):
    track: TrackOut | None = None
    position_s: float = 0.0
    started_at: datetime | None = None


class RoomState(BaseModel):
    """One snapshot, everything on the page."""

    token: str
    name: str
    settings: RoomSettings
    stream_url: str
    me: ListenerInfo
    is_host: bool
    listeners: int
    now_playing: NowPlaying
    queue: list[TrackOut]
    history: list[TrackOut]


# --- Queue ------------------------------------------------------------------
class TrackAdd(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class VoteIn(BaseModel):
    value: int = Field(ge=-1, le=1)


class SearchResult(BaseModel):
    """One hit from the music search, ready to be queued by id."""

    youtube_id: str
    title: str
    duration_s: int
    thumbnail_url: str | None = None
    channel: str | None = None
    # Over the room's track length limit: shown, but adding it would be
    # rejected, so the UI says so instead of letting the listener find out.
    too_long: bool = False
    # Already queued or playing, which the add endpoint rejects as a duplicate.
    queued: bool = False


# --- Chat -------------------------------------------------------------------
class ChatPost(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class ChatMessage(BaseModel):
    """One line of chat, exactly as it travels over the socket."""

    id: str
    name: str
    # Seed for the sender's generated avatar, not an identifier.
    avatar: str
    text: str
    at: str


# --- Moderation -------------------------------------------------------------
class BanIn(BaseModel):
    listener_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=140)


class ListenerRow(BaseModel):
    id: uuid.UUID
    display_name: str
    # Seed for their generated cat; the list is rendered client-side.
    avatar: str
    is_host: bool
    online: bool
    queued: int
    shadow_banned: bool


# --- Meta -------------------------------------------------------------------
class MetaOut(BaseModel):
    app_name: str
    version: str
    seo_enabled: bool
    seo_site_name: str
    seo_description: str
    imprint_enabled: bool
    data_location: str
    contact_links: list[dict]
    contact_email: list[str] | None = None
    license_email: list[str] | None = None
