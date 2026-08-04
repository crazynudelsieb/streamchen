"""ffmpeg plumbing.

The stream must not drop between songs, so a room owns *one* long-lived
encoder connected to Icecast, fed raw PCM on stdin:

    track file -> [decoder ffmpeg] -> s16le PCM -> [encoder ffmpeg] -> Icecast

The encoder reads with ``-re``, so it consumes PCM at exactly real time. That
one flag is the whole pacing mechanism: the decoder blocks on a full pipe
instead of racing ahead, and when nothing is queued the worker writes silence
at the same rate, keeping the mount alive and every listener connected.

The decoder is also where each track is levelled to a common loudness
(app/worker/loudness.py) — it is the only stage that sees one track at a time,
which is what levelling has to be done per.

Command construction is kept separate from process handling so it can be
tested without ffmpeg installed.
"""

from __future__ import annotations

from app.config import Settings
from app.worker.loudness import Loudness, normalize_filter


def icecast_url(settings: Settings, mount: str) -> str:
    return (
        f"icecast://source:{settings.icecast_source_password}"
        f"@{settings.icecast_host}:{settings.icecast_port}{mount}"
    )


def encoder_command(settings: Settings, mount: str, station_name: str) -> list[str]:
    """The per-room source client. Started once, kept for the room's life."""
    return [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-loglevel", "error",
        # Pace the input at real time -- this is what keeps the encoder from
        # sprinting through the pipe and what makes writes upstream block.
        "-re",
        "-f", "s16le",
        "-ar", str(settings.audio_sample_rate),
        "-ac", str(settings.audio_channels),
        "-i", "pipe:0",
        "-c:a", "libmp3lame",
        "-b:a", f"{settings.icecast_bitrate_kbps}k",
        # Hand every frame to the mount as it is encoded. Without this the
        # muxer fills its own output buffer before writing anything, and at
        # these bitrates that buffer is seconds of audio: measured against a
        # sink fed silence at real time, the first bytes leave ffmpeg after
        # 0.1 s with this flag and had still not left after eight seconds
        # without it. Every one of those seconds is added to how long a mount
        # takes to appear, and paid again by every listener who presses play.
        "-flush_packets", "1",
        "-content_type", "audio/mpeg",
        "-ice_name", station_name,
        "-ice_description", "streamchen collaborative radio",
        "-ice_public", "0",
        "-legacy_icecast", "1",
        "-f", "mp3",
        icecast_url(settings, mount),
    ]


def decoder_command(
    settings: Settings, source: str, measured: Loudness | None = None
) -> list[str]:
    """Decode one track (local file or URL) to the encoder's PCM format.

    ``measured`` is what the analysis pass found out about this file, when
    there was time to run one. Without it the track is still levelled, just
    from a standing start — see ``loudness.normalize_filter``.
    """
    levelling = ["-af", normalize_filter(settings, measured)] if settings.audio_normalize else []
    return [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", source,
        "-vn",
        *levelling,
        "-f", "s16le",
        "-ar", str(settings.audio_sample_rate),
        "-ac", str(settings.audio_channels),
        "pipe:1",
    ]


def bytes_per_second(settings: Settings) -> int:
    """s16le: two bytes per sample, per channel."""
    return settings.audio_sample_rate * settings.audio_channels * 2


def seconds_of(byte_count: int, settings: Settings) -> float:
    return byte_count / bytes_per_second(settings)


def silence_chunk(settings: Settings, seconds: float = 0.25) -> bytes:
    """A block of digital silence, sized in whole frames."""
    frame = settings.audio_channels * 2
    frames = max(1, int(settings.audio_sample_rate * seconds))
    return b"\x00" * (frames * frame)
