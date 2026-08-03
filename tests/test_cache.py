"""Audio cache retention (concept §12): nothing survives playback."""

from __future__ import annotations

import os
import time

from app.worker.cache import AudioCache


def make_cache(tmp_path, budget=1024, ttl=600) -> AudioCache:
    return AudioCache(directory=tmp_path / "audio", budget_bytes=budget, ttl_s=ttl)


def write(cache: AudioCache, key: str, size: int = 100, age_s: float = 0) -> None:
    path = cache.directory / f"{key}.webm"
    path.write_bytes(b"\0" * size)
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))


def test_directory_is_created(tmp_path):
    cache = make_cache(tmp_path)
    assert cache.directory.is_dir()


def test_find_locates_a_file_by_id_regardless_of_extension(tmp_path):
    cache = make_cache(tmp_path)
    (cache.directory / "abc123.m4a").write_bytes(b"x")
    assert cache.has("abc123")
    assert cache.find("abc123").name == "abc123.m4a"


def test_missing_track_is_not_found(tmp_path):
    assert make_cache(tmp_path).find("nothing") is None


def test_a_download_still_running_is_not_a_cached_track(tmp_path):
    """One worker serves several rooms out of this directory, so a room can ask
    for a track while another room's download of it is still going. Half a file
    shares the stem on purpose — the sweeps have to spare it — but handing it
    back as cached decodes a truncated song."""
    cache = make_cache(tmp_path)
    (cache.directory / "abc123.webm.part").write_bytes(b"half a song")

    assert cache.find("abc123") is None
    assert not cache.has("abc123")


def test_a_finished_download_wins_over_the_partial_beside_it(tmp_path):
    cache = make_cache(tmp_path)
    (cache.directory / "abc123.part").write_bytes(b"leftover")
    (cache.directory / "abc123.webm").write_bytes(b"the whole song")

    assert cache.find("abc123").name == "abc123.webm"


def test_a_partial_file_is_still_swept_and_released(tmp_path):
    """Not being findable must not make it immortal: concept §12 is about what
    is on disk, not about what anyone asked for."""
    cache = make_cache(tmp_path, ttl=600)
    (cache.directory / "abandoned.part").write_bytes(b"half")
    stamp = time.time() - 3600
    os.utime(cache.directory / "abandoned.part", (stamp, stamp))

    assert cache.sweep() == 1
    assert cache.files() == []


def test_release_deletes_the_track_immediately(tmp_path):
    cache = make_cache(tmp_path)
    write(cache, "played")
    cache.release("played")
    assert not cache.has("played")


def test_sweep_removes_expired_files_but_keeps_the_protected_ones(tmp_path):
    cache = make_cache(tmp_path, ttl=600)
    write(cache, "stale", age_s=3600)
    write(cache, "current", age_s=3600)
    write(cache, "fresh", age_s=10)

    removed = cache.sweep(keep=("current",))

    assert removed == 1
    assert not cache.has("stale")
    assert cache.has("current")
    assert cache.has("fresh")


def test_budget_evicts_oldest_first(tmp_path):
    cache = make_cache(tmp_path, budget=250)
    write(cache, "oldest", size=100, age_s=300)
    write(cache, "middle", size=100, age_s=200)
    write(cache, "newest", size=100, age_s=100)

    cache.enforce_budget()

    assert not cache.has("oldest")
    assert cache.total_bytes() <= 250


def test_budget_never_evicts_the_track_being_played(tmp_path):
    cache = make_cache(tmp_path, budget=50)
    write(cache, "playing", size=100, age_s=900)
    write(cache, "other", size=100, age_s=10)

    cache.enforce_budget(keep=("playing",))

    assert cache.has("playing")
    assert not cache.has("other")


def test_budget_never_evicts_another_rooms_next_track(tmp_path):
    """One worker plays several rooms out of one directory, so eviction has to
    know about all of them: taking the file the room next door is about to play
    is exactly the stall the prefetch exists to avoid."""
    cache = make_cache(tmp_path, budget=50)
    cache.protected = lambda: {"next-door"}
    write(cache, "next-door", size=100, age_s=900)
    write(cache, "nobodys", size=100, age_s=10)

    cache.enforce_budget(keep=())

    assert cache.has("next-door")
    assert not cache.has("nobodys")


def test_sweep_never_expires_another_rooms_next_track(tmp_path):
    cache = make_cache(tmp_path, ttl=600)
    cache.protected = lambda: {"next-door"}
    write(cache, "next-door", age_s=3600)
    write(cache, "stale", age_s=3600)

    assert cache.sweep() == 1
    assert cache.has("next-door")
    assert not cache.has("stale")


def test_purge_empties_the_directory(tmp_path):
    cache = make_cache(tmp_path)
    for key in ("a", "b", "c"):
        write(cache, key)

    assert cache.purge() == 3
    assert cache.files() == []
