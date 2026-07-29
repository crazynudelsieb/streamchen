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


def test_purge_empties_the_directory(tmp_path):
    cache = make_cache(tmp_path)
    for key in ("a", "b", "c"):
        write(cache, key)

    assert cache.purge() == 3
    assert cache.files() == []
