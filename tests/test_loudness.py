"""Levelling every source to one target — checked without ffmpeg installed.

What matters here is not that loudnorm works, but that the two passes agree
with each other, that an unmeasurable file still plays, and that one file is
never analysed twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from app.config import Settings
from app.worker.loudness import (
    Loudness,
    Measurements,
    measure_command,
    normalize_filter,
    parse_measurement,
)


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite+aiosqlite://",
        "redis_url": "redis://localhost:6379/0",
        "base_url": "http://test",
        "icecast_source_password": "s3cret",
    }
    return Settings(**(base | overrides))


def ffmpeg_output(**overrides) -> str:
    """What the analysis pass prints: the decoder's chatter, then the JSON."""
    reported = {
        "input_i": "-8.42",
        "input_tp": "0.35",
        "input_lra": "4.20",
        "input_thresh": "-18.70",
        "output_i": "-14.02",
        "output_tp": "-1.50",
        "output_lra": "4.10",
        "output_thresh": "-24.30",
        "normalization_type": "dynamic",
        "target_offset": "-0.12",
    }
    return (
        "Input #0, mp3, from '/tmp/audio/abc.mp3':\n"
        "  Duration: 00:03:41.20, bitrate: 128 kb/s\n"
        "[Parsed_loudnorm_0 @ 0x5566] \n" + json.dumps(reported | overrides, indent=2) + "\n"
    )


# --- The two passes have to agree -------------------------------------------
def test_both_passes_aim_at_the_same_target():
    """target_offset is measured against whatever target the analysis pass was
    given, so analysing at one and playing at another mis-levels every track by
    the difference — and both commands still look correct on their own."""
    settings = make_settings(audio_target_lufs=-16.0, audio_target_tp=-2.0)
    measured = parse_measurement(ffmpeg_output())

    analysis = measure_command(settings, "/tmp/audio/abc.mp3")
    playback = normalize_filter(settings, measured)

    for target in ("I=-16", "TP=-2", "LRA=20"):
        assert target in analysis[analysis.index("-af") + 1]
        assert target in playback


def test_the_loudness_range_target_leaves_ordinary_music_alone():
    """The correction stays one fixed gain only while a track's own range fits
    inside this target; past it loudnorm compresses the range to fit and the
    track audibly changes character. A mildly dynamic mix measures 12-13 LU, so
    the broadcast convention of 11 would make the compressed path the common
    one — levelling that does not level so much as squash."""
    assert make_settings().audio_target_lra >= 15


def test_the_analysis_pass_asks_for_json_it_can_read_back():
    command = measure_command(make_settings(), "/tmp/audio/abc.mp3")

    assert "print_format=json" in command[command.index("-af") + 1]
    # loudnorm prints its result at info level; -loglevel error would throw
    # away the only output the pass exists for.
    assert command[command.index("-loglevel") + 1] == "info"
    # Nothing is encoded: the pass exists for what it printed on the way past.
    assert command[-3:] == ["-f", "null", "-"]


def test_the_analysis_pass_decodes_nothing_it_does_not_need():
    assert "-vn" in measure_command(make_settings(), "/tmp/audio/abc.mp3")


# --- Reading what ffmpeg reported -------------------------------------------
def test_the_measurements_are_read_off_the_analysis_pass():
    measured = parse_measurement(ffmpeg_output())

    assert measured == Loudness(
        input_i=-8.42,
        input_tp=0.35,
        input_lra=4.20,
        input_thresh=-18.70,
        target_offset=-0.12,
    )


def test_a_silent_file_measures_as_nothing_usable():
    """Silence measures as -inf, which is not a number a filter argument can
    carry — and there is nothing in it to level anyway."""
    assert parse_measurement(ffmpeg_output(input_i="-inf", input_thresh="-inf")) is None


def test_output_without_json_is_not_a_measurement():
    assert parse_measurement("") is None
    assert parse_measurement("Invalid data found when processing input\n") is None


def test_json_missing_a_field_is_not_a_measurement():
    """loudnorm ignores the measured set unless it is complete, so a partial
    one would silently level the track from a standing start while looking
    like it had been measured."""
    output = ffmpeg_output()
    reported = json.loads(output[output.index("{") :])
    del reported["input_thresh"]

    assert parse_measurement(json.dumps(reported)) is None


# --- The filter the decoder runs --------------------------------------------
def test_a_measured_track_is_levelled_by_one_fixed_gain():
    """The point of measuring first: the correction is a single gain over the
    whole track rather than a filter feeling its way to the target."""
    filter_chain = normalize_filter(make_settings(), parse_measurement(ffmpeg_output()))

    assert "measured_I=-8.42" in filter_chain
    assert "measured_TP=0.35" in filter_chain
    assert "measured_LRA=4.2" in filter_chain
    assert "measured_thresh=-18.7" in filter_chain
    assert "offset=-0.12" in filter_chain
    assert "linear=true" in filter_chain


def test_an_unmeasured_track_is_still_levelled():
    """A cold start has no analysis behind it. One-pass loudnorm still lands
    near the target, which is a great deal closer than not levelling at all."""
    filter_chain = normalize_filter(make_settings(), None)

    assert filter_chain.startswith("loudnorm=I=-14")
    assert "measured_" not in filter_chain


def test_the_filter_hands_the_encoder_the_rate_it_expects():
    """loudnorm works at 192 kHz internally, so something has to resample
    before the PCM reaches an encoder that was told 44100."""
    settings = make_settings(audio_sample_rate=48000)

    assert normalize_filter(settings, None).endswith("aresample=48000")


# --- One analysis per file ---------------------------------------------------
class FakeProbe:
    """Stands in for the ffmpeg analysis pass, counting what it was asked."""

    def __init__(self, result: Loudness | None = None, delay_s: float = 0.0) -> None:
        self.result = result
        self.delay_s = delay_s
        self.calls: list[Path] = []

    async def __call__(self, path: Path) -> Loudness | None:
        self.calls.append(path)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.result


def make_measurements(probe: FakeProbe, **overrides) -> Measurements:
    measurements = Measurements(make_settings(**overrides))
    measurements._probe = probe
    return measurements


async def test_a_file_is_only_ever_analysed_once():
    probe = FakeProbe(parse_measurement(ffmpeg_output()))
    measurements = make_measurements(probe)

    first = await measurements.measure("abc", Path("/tmp/audio/abc.mp3"))
    second = await measurements.measure("abc", Path("/tmp/audio/abc.mp3"))

    assert first == second
    assert len(probe.calls) == 1


async def test_a_second_caller_waits_for_the_analysis_already_running():
    """The lookahead prepares a decoder for the same track it prefetched, and
    the boundary may ask again — none of which is a reason to decode the file
    three times."""
    probe = FakeProbe(parse_measurement(ffmpeg_output()), delay_s=0.05)
    measurements = make_measurements(probe)
    path = Path("/tmp/audio/abc.mp3")

    results = await asyncio.gather(*(measurements.measure("abc", path) for _ in range(3)))

    assert len(probe.calls) == 1
    assert results[0] is not None
    assert results[0] == results[1] == results[2]


async def test_a_file_that_cannot_be_measured_is_not_measured_again():
    """Otherwise the lookahead re-decodes it every couple of seconds for as
    long as it sits at the top of the queue."""
    probe = FakeProbe(None)
    measurements = make_measurements(probe)

    assert await measurements.measure("abc", Path("/tmp/audio/abc.mp3")) is None
    assert await measurements.measure("abc", Path("/tmp/audio/abc.mp3")) is None
    assert len(probe.calls) == 1


async def test_what_was_measured_is_there_without_waiting():
    """The decoder opens at the boundary, where there is no time to wait for
    anything at all."""
    probe = FakeProbe(parse_measurement(ffmpeg_output()))
    measurements = make_measurements(probe)

    assert measurements.known("abc") is None
    await measurements.measure("abc", Path("/tmp/audio/abc.mp3"))
    assert measurements.known("abc") is not None


async def test_a_released_file_is_forgotten_with_its_audio():
    """A worker runs for weeks. Remembering every track it ever played is a
    leak, and the numbers describe a file that no longer exists."""
    probe = FakeProbe(parse_measurement(ffmpeg_output()))
    measurements = make_measurements(probe)

    await measurements.measure("abc", Path("/tmp/audio/abc.mp3"))
    measurements.forget("abc")

    assert measurements.known("abc") is None
    assert measurements._known == {}


async def test_a_cancelled_caller_does_not_take_the_analysis_with_it():
    """A track ending cancels the lookahead mid-analysis. Throwing away a
    decode that is nearly done is one the boundary then pays for again."""
    probe = FakeProbe(parse_measurement(ffmpeg_output()), delay_s=0.05)
    measurements = make_measurements(probe)
    path = Path("/tmp/audio/abc.mp3")

    waiting = asyncio.create_task(measurements.measure("abc", path))
    await asyncio.sleep(0.01)
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    assert await measurements.measure("abc", path) is not None
    assert len(probe.calls) == 1


async def test_levelling_can_be_turned_off_entirely():
    """An operator feeding the room from an already-mastered library has no
    use for any of this, and it costs a decode a track."""
    probe = FakeProbe(parse_measurement(ffmpeg_output()))
    measurements = make_measurements(probe, audio_normalize=False)

    assert await measurements.measure("abc", Path("/tmp/audio/abc.mp3")) is None
    assert probe.calls == []
