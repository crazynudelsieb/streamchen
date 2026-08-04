"""Database models — the single source of truth for room state.

Nothing audio-related is stored here: a track row is a *reference* to a
YouTube video plus who queued it and how it scored. Media never reaches the
database (concept §12).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


def utcnow() -> datetime:
    """Timezone-aware now. Used as the Python-side default everywhere so the
    value does not depend on the database server's clock or dialect."""
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat those as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Base(DeclarativeBase):
    pass


# Track lifecycle. Plain strings rather than a database enum: the set changes
# with the app, not with the schema, and it keeps SQLite (tests) and Postgres
# (production) on identical DDL.
STATE_QUEUED = "queued"
STATE_PLAYING = "playing"
STATE_PLAYED = "played"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"


class Room(Base):
    """A shared listening room. The token *is* the invite link."""

    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))

    # Argon2 hash. The plaintext is shown to the host exactly once, at creation,
    # and never leaves their browser afterwards (concept §14).
    host_secret_hash: Mapped[str] = mapped_column(String(255))

    # --- Settings (concept §6) ---
    voting_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    queue_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    # The host's stop. A room without a source is still a room: its queue, its
    # listeners, its name and above all its link survive, and starting the
    # stream again picks up where it left off.
    #
    # This is also the manual override of the automatic behaviour. A stream
    # normally follows the listeners — it starts when somebody arrives and
    # stops when the last one leaves — but a host who stopped it means it,
    # so nobody's arrival starts it again until they say so.
    stream_stopped: Mapped[bool] = mapped_column(Boolean, default=False)

    chat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_pending_per_listener: Mapped[int] = mapped_column(Integer, default=3)
    max_listeners: Mapped[int] = mapped_column(Integer, default=100)
    fallback_playlist: Mapped[str | None] = mapped_column(Text, default=None)

    # --- News (app/news.py) ---
    # Off unless a host asks for it: a room is music by default, and a bulletin
    # is the one thing on the stream nobody in the room queued.
    news_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    news_interval_min: Mapped[int] = mapped_column(Integer, default=60)
    # When this room last heard a bulletin, and which one. The first is the
    # cadence; the second is what stops a feed that has not moved on being
    # played twice.
    news_last_played_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    news_last_episode: Mapped[str | None] = mapped_column(String(200), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # passive_deletes: the FKs already carry ON DELETE CASCADE, so deleting a
    # room is one statement rather than a load-then-delete of everything in it.
    tracks: Mapped[list[Track]] = relationship(
        back_populates="room", cascade="all, delete-orphan", passive_deletes=True
    )
    listeners: Mapped[list[Listener]] = relationship(
        back_populates="room", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def mount(self) -> str:
        """Icecast mount point for this room's stream."""
        return f"/{self.token}.mp3"


class Listener(Base):
    """An anonymous participant, identified only by their session cookie."""

    __tablename__ = "listeners"
    __table_args__ = (
        UniqueConstraint("room_id", "session_id", name="uq_listener_room_session"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    display_name: Mapped[str] = mapped_column(String(40))
    is_host: Mapped[bool] = mapped_column(Boolean, default=False)

    # Normally None, meaning "the cat your id hashes to" — which is what makes
    # an avatar stable without storing anything. Set only when a listener asks
    # for a different one, and then this seed is drawn instead (app/avatars.py).
    chosen_avatar: Mapped[str | None] = mapped_column(String(32), default=None)

    # Set when the listener trips the spam thresholds. Their submissions still
    # appear to them and go nowhere else (concept §10).
    shadow_banned_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    room: Mapped[Room] = relationship(back_populates="listeners")
    # Removing a listener removes what they queued. Without the cascade
    # SQLAlchemy would try to orphan those rows by nulling a NOT NULL column.
    tracks: Mapped[list[Track]] = relationship(
        back_populates="added_by", cascade="all, delete-orphan", passive_deletes=True
    )

    def is_shadow_banned(self, now: datetime | None = None) -> bool:
        until = as_utc(self.shadow_banned_until)
        if until is None:
            return False
        return until > (now or utcnow())

    def shadow_ban(self, minutes: int) -> None:
        self.shadow_banned_until = utcnow() + timedelta(minutes=minutes)


class Ban(Base):
    """A host-issued ban. Keyed by session rather than by listener row so it
    survives the listener being cleaned up and still bites on rejoin."""

    __tablename__ = "bans"
    __table_args__ = (UniqueConstraint("room_id", "session_id", name="uq_ban_room_session"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    reason: Mapped[str | None] = mapped_column(String(140), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Track(Base):
    """One queue entry."""

    __tablename__ = "tracks"
    __table_args__ = (Index("ix_track_room_state", "room_id", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), index=True
    )
    youtube_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_url: Mapped[str | None] = mapped_column(String(400), default=None)
    channel: Mapped[str | None] = mapped_column(String(120), default=None)

    added_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listeners.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(16), default=STATE_QUEUED)

    # A shadow-banned listener's submission: stored, shown back to them, never
    # served to anyone else and never played.
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)

    # Host reordering. 0 is "normal, subject to fair scheduling"; anything
    # higher jumps the queue, highest first.
    priority: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(String(200), default=None)

    room: Mapped[Room] = relationship(back_populates="tracks")
    added_by: Mapped[Listener] = relationship(back_populates="tracks")
    votes: Mapped[list[Vote]] = relationship(
        back_populates="track", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def score(self) -> int:
        """upvotes - downvotes (concept §9)."""
        return sum(vote.value for vote in self.votes)

    @property
    def upvotes(self) -> int:
        return sum(1 for vote in self.votes if vote.value > 0)

    @property
    def downvotes(self) -> int:
        return sum(1 for vote in self.votes if vote.value < 0)


class Vote(Base):
    """One listener's verdict on one track. Re-voting overwrites."""

    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("track_id", "listener_id", name="uq_vote_track_listener"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    listener_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listeners.id", ondelete="CASCADE"), index=True
    )
    value: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    track: Mapped[Track] = relationship(back_populates="votes")
