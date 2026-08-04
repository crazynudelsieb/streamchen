"""ffmpeg command construction — checked without ffmpeg installed."""

from __future__ import annotations

from app.config import Settings
from app.worker.loudness import Loudness
from app.worker.pipeline import (
    bytes_per_second,
    decoder_command,
    encoder_command,
    icecast_url,
    seconds_of,
    silence_chunk,
)


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite+aiosqlite://",
        "redis_url": "redis://localhost:6379/0",
        "base_url": "http://test",
        "icecast_host": "icecast",
        "icecast_port": 8000,
        "icecast_source_password": "s3cret",
    }
    return Settings(**(base | overrides))


def test_the_encoder_paces_itself_at_real_time():
    """Without -re the encoder races through the pipe and the stream drifts."""
    command = encoder_command(make_settings(), "/room.mp3", "Test Room")
    assert "-re" in command
    assert command[command.index("-i") + 1] == "pipe:0"


def test_the_encoder_targets_the_rooms_own_mount():
    command = encoder_command(make_settings(), "/abc123.mp3", "Test Room")
    assert command[-1] == "icecast://source:s3cret@icecast:8000/abc123.mp3"


def test_the_encoder_announces_the_room_name_on_the_mount():
    command = encoder_command(make_settings(), "/room.mp3", "Kitchen Radio")
    assert command[command.index("-ice_name") + 1] == "Kitchen Radio"


def test_the_encoder_hands_every_frame_straight_to_icecast():
    """Buffered output is latency every listener pays, at every start: the
    muxer holds seconds of audio back before writing any of it, which is added
    to how long the mount takes to exist and to how far behind live it runs."""
    command = encoder_command(make_settings(), "/room.mp3", "Test")

    flag = command.index("-flush_packets")
    assert command[flag + 1] == "1"
    # An output option: before the muxer, after the input it applies to.
    assert flag > command.index("-i")


def test_the_encoder_serves_mp3():
    command = encoder_command(make_settings(), "/room.mp3", "Test")
    assert command[command.index("-content_type") + 1] == "audio/mpeg"
    assert command[command.index("-c:a") + 1] == "libmp3lame"


def test_the_decoder_emits_exactly_what_the_encoder_expects():
    settings = make_settings()
    decoder = decoder_command(settings, "/tmp/track.webm")
    encoder = encoder_command(settings, "/room.mp3", "Test")

    for flag in ("-f", "-ar", "-ac"):
        assert decoder[decoder.index(flag) + 1] == encoder[encoder.index(flag) + 1]


def test_the_decoder_drops_video():
    assert "-vn" in decoder_command(make_settings(), "/tmp/track.webm")


def test_the_decoder_levels_every_track_to_the_same_loudness():
    """The stage that sees one track at a time is the only one that can."""
    command = decoder_command(make_settings(), "/tmp/track.webm")

    assert command[command.index("-af") + 1].startswith("loudnorm=")


def test_the_decoder_uses_what_the_analysis_pass_measured():
    measured = Loudness(
        input_i=-8.42, input_tp=0.35, input_lra=4.2, input_thresh=-18.7, target_offset=-0.12
    )
    command = decoder_command(make_settings(), "/tmp/track.webm", measured)

    assert "measured_I=-8.42" in command[command.index("-af") + 1]


def test_levelling_can_be_turned_off():
    command = decoder_command(make_settings(audio_normalize=False), "/tmp/track.webm")

    assert "-af" not in command


def test_the_source_password_is_only_in_the_icecast_url():
    settings = make_settings()
    assert "s3cret" in icecast_url(settings, "/room.mp3")
    assert "s3cret" not in " ".join(decoder_command(settings, "/tmp/x.webm"))


def test_pcm_maths_agree_with_each_other():
    settings = make_settings()
    one_second = bytes_per_second(settings)

    assert one_second == 44100 * 2 * 2
    assert seconds_of(one_second * 5, settings) == 5.0


def test_silence_is_whole_frames_of_nothing():
    settings = make_settings()
    chunk = silence_chunk(settings, 0.5)

    assert set(chunk) == {0}
    assert len(chunk) % (settings.audio_channels * 2) == 0
    assert abs(seconds_of(len(chunk), settings) - 0.5) < 0.01
