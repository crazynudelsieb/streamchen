"""Application configuration.

Everything is read from the environment once, at import of the first
``get_settings()`` call, and handed around as a frozen object. The API and the
worker load the *same* settings object so a limit can never mean one thing to
the API and another to the worker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"{key} environment variable must be set")
    return value


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    return int(raw) if raw else default


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _csv(key: str) -> list[str]:
    raw = os.environ.get(key, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _nl(value: str) -> str:
    """Allow ``\\n`` in single-line env values to mean a real line break."""
    return value.replace("\\n", "\n")


@dataclass(frozen=True)
class Settings:
    """Immutable view of the environment."""

    # --- Core ---------------------------------------------------------
    app_name: str = "streamchen"
    database_url: str = ""
    redis_url: str = ""
    base_url: str = ""
    log_level: str = "INFO"
    cors_origins: list[str] = field(default_factory=list)

    # --- Sessions / security -----------------------------------------
    session_cookie: str = "sc_session"
    csrf_cookie: str = "sc_csrf"
    secure_cookies: bool = False
    session_days: int = 90
    trusted_proxy_count: int = 1

    # --- Room defaults (a host can tighten these per room) -------------
    room_name_max_length: int = 80
    default_max_listeners: int = 100
    default_max_pending_per_listener: int = 3
    room_idle_days: int = 7

    # --- Spam protection (concept §10) --------------------------------
    add_rate_window_s: int = 15
    add_rate_limit: int = 1
    vote_rate_window_s: int = 10
    vote_rate_limit: int = 5
    shadow_ban_minutes: int = 15
    shadow_ban_strikes: int = 5
    strike_window_s: int = 300

    # --- Caches (concept §12) -----------------------------------------
    metadata_cache_ttl_s: int = 24 * 3600
    stream_url_cache_ttl_s: int = 300
    audio_cache_dir: str = "/tmp/streamchen-audio"
    audio_cache_budget_bytes: int = 1024 * 1024 * 1024
    audio_prefetch_ttl_s: int = 600
    max_track_duration_s: int = 900

    # --- Icecast / playback -------------------------------------------
    icecast_host: str = "icecast"
    icecast_port: int = 8000
    icecast_source_password: str = "hackme"
    icecast_public_url: str = ""
    icecast_bitrate_kbps: int = 128
    audio_sample_rate: int = 44100
    audio_channels: int = 2
    ffmpeg_binary: str = "ffmpeg"
    worker_poll_interval_s: int = 2
    worker_lock_ttl_s: int = 30

    # --- Footer / legal (shared appchen standard) ----------------------
    contact_email: str = ""
    contact_mastodon: str = ""
    contact_github: str = ""
    contact_kofi: str = ""
    contact_buymeacoffee: str = ""
    imprint_name: str = ""
    imprint_address: str = ""
    imprint_email: str = ""
    imprint_phone: str = ""
    imprint_vat: str = ""
    imprint_extra: str = ""
    data_location: str = ""

    # --- SEO -----------------------------------------------------------
    seo_enabled: bool = False
    seo_site_name: str = "streamchen"
    seo_description: str = (
        "streamchen — a private online radio you share with a link. "
        "Everyone hears the same stream, everyone adds songs, everyone votes."
    )

    @property
    def imprint_enabled(self) -> bool:
        return bool(self.imprint_name)

    @property
    def imprint_contact_email(self) -> str:
        """Operators who use one address for everything set CONTACT_EMAIL only."""
        return self.imprint_email or self.contact_email

    @property
    def stream_base_url(self) -> str:
        """Public base for Icecast mounts, used to build player URLs."""
        if self.icecast_public_url:
            return self.icecast_public_url.rstrip("/")
        return f"http://{self.icecast_host}:{self.icecast_port}"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_name=_str("APP_NAME", "streamchen"),
            database_url=_require("DATABASE_URL"),
            redis_url=_require("REDIS_URL"),
            base_url=_require("BASE_URL").rstrip("/"),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            cors_origins=_csv("CORS_ORIGINS"),
            secure_cookies=_bool("SECURE_COOKIES", False),
            session_days=_int("SESSION_DAYS", 90),
            trusted_proxy_count=_int("TRUSTED_PROXY_COUNT", 1),
            default_max_listeners=_int("MAX_LISTENERS", 100),
            default_max_pending_per_listener=_int("MAX_PENDING_PER_LISTENER", 3),
            room_idle_days=_int("ROOM_IDLE_DAYS", 7),
            add_rate_window_s=_int("ADD_RATE_WINDOW_S", 15),
            add_rate_limit=_int("ADD_RATE_LIMIT", 1),
            vote_rate_window_s=_int("VOTE_RATE_WINDOW_S", 10),
            vote_rate_limit=_int("VOTE_RATE_LIMIT", 5),
            shadow_ban_minutes=_int("SHADOW_BAN_MINUTES", 15),
            shadow_ban_strikes=_int("SHADOW_BAN_STRIKES", 5),
            metadata_cache_ttl_s=_int("METADATA_CACHE_TTL_S", 24 * 3600),
            stream_url_cache_ttl_s=_int("STREAM_URL_CACHE_TTL_S", 300),
            audio_cache_dir=_str("AUDIO_CACHE_DIR", "/tmp/streamchen-audio"),
            audio_cache_budget_bytes=_int("AUDIO_CACHE_BUDGET_BYTES", 1024 * 1024 * 1024),
            audio_prefetch_ttl_s=_int("AUDIO_PREFETCH_TTL_S", 600),
            max_track_duration_s=_int("MAX_TRACK_DURATION_S", 900),
            icecast_host=_str("ICECAST_HOST", "icecast"),
            icecast_port=_int("ICECAST_PORT", 8000),
            icecast_source_password=_str("ICECAST_SOURCE_PASSWORD", "hackme"),
            icecast_public_url=_str("ICECAST_PUBLIC_URL", ""),
            icecast_bitrate_kbps=_int("ICECAST_BITRATE_KBPS", 128),
            ffmpeg_binary=_str("FFMPEG_BINARY", "ffmpeg"),
            worker_poll_interval_s=_int("WORKER_POLL_INTERVAL_S", 2),
            contact_email=_str("CONTACT_EMAIL"),
            contact_mastodon=_str("CONTACT_MASTODON"),
            contact_github=_str("CONTACT_GITHUB"),
            contact_kofi=_str("CONTACT_KOFI"),
            contact_buymeacoffee=_str("CONTACT_BUYMEACOFFEE"),
            imprint_name=_str("IMPRINT_NAME"),
            imprint_address=_nl(_str("IMPRINT_ADDRESS")),
            imprint_email=_str("IMPRINT_EMAIL"),
            imprint_phone=_str("IMPRINT_PHONE"),
            imprint_vat=_str("IMPRINT_VAT"),
            imprint_extra=_nl(_str("IMPRINT_EXTRA")),
            data_location=_str("DATA_LOCATION"),
            seo_enabled=_bool("SEO_ENABLED", False),
            seo_site_name=_str("SEO_SITE_NAME", "streamchen"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. ``get_settings.cache_clear()`` in tests."""
    return Settings.from_env()
