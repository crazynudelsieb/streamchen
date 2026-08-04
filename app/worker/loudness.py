"""Levelling every source to one loudness target (EBU R128).

Audio arrives from wherever a listener found it: a loudness-war single, a quiet
live upload, a news bulletin cut by a broadcaster to its own house level.
Played back to back untouched they land at wildly different volumes, and the
only remedy a listener has is the one volume control in their browser — which
they then have to move again at the next handover.

So every source is measured against a fixed target before it plays. ffmpeg's
``loudnorm`` can do that in a single pass, but only by adapting as it goes: it
cannot know how loud a track is until it has heard it, so the opening seconds
are levelled against a guess and the filter compresses its way to the target
afterwards. Measuring first and handing the numbers to the playback pass makes
the correction one fixed gain across the whole track — inaudible as an effect,
which is the entire point of doing it twice.

The second pass is only worth having because the first one is free: the file is
on disk minutes before it is due (``player._lookahead_step``), so the analysis
runs while something else is on air and costs nobody a gap. When there was no
such window — a cold start, an unreadable file, no ffmpeg at all — playback
falls back to the one-pass filter, which still lands near the target.

Command construction and parsing are kept free of process handling so they can
be tested without ffmpeg installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.worker.tasks import SharedTasks

logger = logging.getLogger(__name__)

# What the analysis pass reports and the playback pass needs back. All five, or
# the measurement is no use: loudnorm ignores the whole measured set unless it
# is complete.
MEASURED_FIELDS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


@dataclass(frozen=True)
class Loudness:
    """What one file measured, in the units loudnorm reports them in."""

    input_i: float  # integrated loudness, LUFS
    input_tp: float  # true peak, dBTP
    input_lra: float  # loudness range, LU
    input_thresh: float  # gating threshold, LUFS
    target_offset: float  # LU the second pass has to make up


def _targets(settings: Settings) -> str:
    """The target the analysis and the playback pass must agree on.

    ``target_offset`` is measured *against* these, so analysing at one target
    and playing at another mis-levels the track by the difference — silently,
    because both passes still look perfectly correct on their own.
    """
    return (
        f"I={settings.audio_target_lufs:g}"
        f":TP={settings.audio_target_tp:g}"
        f":LRA={settings.audio_target_lra:g}"
    )


def measure_command(settings: Settings, source: str) -> list[str]:
    """The analysis pass: decode the whole file, print what it measured."""
    return [
        settings.ffmpeg_binary,
        "-hide_banner",
        # loudnorm prints its JSON at info level, so the -loglevel error every
        # other call here uses would throw away the only output wanted.
        "-loglevel", "info",
        "-nostdin",
        "-i", source,
        "-vn",
        "-af", f"loudnorm={_targets(settings)}:print_format=json",
        "-f", "null",
        "-",
    ]


def normalize_filter(settings: Settings, measured: Loudness | None) -> str:
    """The ``-af`` chain a playback decode runs the track through."""
    if measured is None:
        # Nothing measured: a cold start, or a file ffmpeg could not read well
        # enough to say. One-pass loudnorm still lands the track near the
        # target, it just gets there by compressing rather than by a fixed gain.
        loudnorm = f"loudnorm={_targets(settings)}"
    else:
        loudnorm = (
            f"loudnorm={_targets(settings)}"
            f":measured_I={measured.input_i:g}"
            f":measured_TP={measured.input_tp:g}"
            f":measured_LRA={measured.input_lra:g}"
            f":measured_thresh={measured.input_thresh:g}"
            f":offset={measured.target_offset:g}"
            # The whole reason for the first pass. loudnorm falls back to its
            # dynamic mode by itself if the fixed gain would clip the ceiling.
            ":linear=true"
        )
    # loudnorm works at 192 kHz internally and says so on its output pad, so
    # something has to resample before the PCM reaches the encoder. Naming it
    # here rather than leaving ffmpeg to insert one keeps the chain identical
    # on every build.
    return f"{loudnorm},aresample={settings.audio_sample_rate}"


def parse_measurement(output: str) -> Loudness | None:
    """The numbers out of ffmpeg's stderr, or None if it gave no usable set.

    The JSON is the last thing printed, after whatever the decoder had to say
    about the file, so it is found from the back.
    """
    start = output.rfind("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        return None

    try:
        reported = json.loads(output[start : end + 1])
        values = {field: float(reported[field]) for field in MEASURED_FIELDS}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    # Silence measures as -inf, which is not a number any filter argument can
    # carry — and there is nothing to level in it anyway.
    if not all(math.isfinite(value) for value in values.values()):
        return None
    return Loudness(**values)


class Measurements:
    """What has been measured, and what is being measured right now.

    Keyed by cache key like the files themselves, so a track that is prefetched,
    prepared and then played is analysed once. Mirrors ``Downloads``: whoever
    asks second waits on the first call rather than starting a second ffmpeg
    over the same file.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # A failure is remembered as None on purpose. Without that, a file
        # ffmpeg cannot read is re-analysed on every lookahead poll for as long
        # as it sits at the top of the queue.
        self._known: dict[str, Loudness | None] = {}
        self._tasks: SharedTasks[Loudness | None] = SharedTasks()

    def known(self, key: str) -> Loudness | None:
        """What was measured for this key, without waiting for anything."""
        return self._known.get(key)

    async def measure(self, key: str, path: Path) -> Loudness | None:
        """Analyse the file unless somebody already has, or already is."""
        if not self._settings.audio_normalize:
            return None
        if key in self._known:
            return self._known[key]
        return await self._tasks.run(key, lambda: self._analyse(key, path))

    def forget(self, key: str) -> None:
        """Drop what is known about a file that is gone.

        Called wherever the audio is released. The numbers describe that file
        and nothing else, and a worker left running for a week would otherwise
        remember every track it had ever played.
        """
        self._known.pop(key, None)
        self._tasks.cancel(key)

    def abandon(self) -> None:
        """Let go of every analysis still running. The ffmpeg processes are
        killed by the tasks themselves as they unwind."""
        self._tasks.abandon()

    async def _analyse(self, key: str, path: Path) -> Loudness | None:
        measured = await self._probe(path)
        self._known[key] = measured
        if measured is None:
            logger.info("could not measure the loudness of %s", path.name)
        return measured

    async def _probe(self, path: Path) -> Loudness | None:
        """Run the analysis pass. Never raises for anything but cancellation:
        a track that cannot be measured is still a track that has to play."""
        try:
            process = await asyncio.create_subprocess_exec(
                *measure_command(self._settings, str(path)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:  # no ffmpeg on PATH, or no room to fork
            logger.info("could not start the loudness analysis: %s", exc)
            return None

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._settings.audio_measure_timeout_s
            )
        except TimeoutError:
            # An hour-long upload, or ffmpeg stuck on something it cannot make
            # sense of. This is a background nicety either way.
            await _kill(process)
            return None
        except asyncio.CancelledError:
            await _kill(process)
            raise

        if process.returncode != 0:
            return None
        return parse_measurement(stderr.decode("utf-8", "replace"))


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        await process.wait()
