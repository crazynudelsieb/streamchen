"""Temporary audio cache (concept §12).

The hard requirement is that no media survives playback. Everything here
exists to make that easy to guarantee:

* one directory, nothing else writes to it;
* a file is deleted the moment its track finishes;
* anything that outlives its TTL is swept, so a crashed worker cannot leave
  media behind;
* a hard disk budget, enforced by evicting the oldest files first.

Pure filesystem operations — no network, no database — so the retention rules
can be tested directly.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# What a download in flight is called, until it is complete and renamed
# (app/worker/download.py).
PARTIAL_SUFFIX = ".part"


@dataclass
class AudioCache:
    directory: Path
    budget_bytes: int
    ttl_s: int

    # Keys nothing may delete, whoever is asking. One worker plays several
    # rooms out of one directory, so without this a busy room's eviction can
    # take the file the room next door is about to play — and that room then
    # pays for the download at exactly the moment it must not.
    protected: Callable[[], set[str]] | None = None

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

        # Checked here rather than at the first download: in a container the
        # cache is a tmpfs mounted over this path, so a mismatch between the
        # mount's owner and the container user shows up as an unreadable
        # directory. Saying so once at startup beats a PermissionError from
        # whichever sweep happens to touch it first.
        if not os.access(self.directory, os.R_OK | os.W_OK | os.X_OK):
            # getuid is POSIX-only, and this has to be able to say what is wrong
            # on a developer's Windows machine too -- an error path that raises
            # its own AttributeError says nothing at all.
            whoami = getattr(os, "getuid", lambda: "this user")()
            raise PermissionError(
                f"audio cache {self.directory} is not readable/writable by "
                f"uid {whoami}: check the tmpfs uid/gid mount options"
            )

    # --- Lookups ---------------------------------------------------------
    def find(self, key: str) -> Path | None:
        """The cached file for a track, whatever extension it landed with.

        A download in flight is not one of them. Both downloaders write to
        ``<key>.part`` first, deliberately sharing the stem so the sweeps spare
        the partial file — but one worker serves several rooms out of this
        directory, so the room next door can ask for a track while another
        room's download of it is still running, and half a file is not
        something to hand back as cached: it decodes to a truncated song, and
        anything measured from it describes audio nobody will hear.
        """
        for path in sorted(self.directory.glob(f"{key}.*")):
            if path.is_file() and path.suffix != PARTIAL_SUFFIX:
                return path
        return None

    def has(self, key: str) -> bool:
        return self.find(key) is not None

    def files(self) -> list[Path]:
        return [path for path in self.directory.iterdir() if path.is_file()]

    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.files())

    # --- Retention -------------------------------------------------------
    def release(self, key: str) -> None:
        """Delete a track's file. Called the instant playback ends."""
        for path in self.directory.glob(f"{key}.*"):
            self._unlink(path)

    def _spared(self, keep: Iterable[str]) -> set[str]:
        """What this deletion pass may not touch: the caller's own keys plus
        every other room's current and next track."""
        spared = set(keep)
        if self.protected is not None:
            spared |= self.protected()
        return spared

    def sweep(self, keep: Iterable[str] = ()) -> int:
        """Delete anything past its TTL. ``keep`` is current + next.

        Concept §12: the current track lives until playback completes, a
        prefetched track for at most ``ttl_s``.
        """
        protected = self._spared(keep)
        cutoff = time.time() - self.ttl_s
        removed = 0
        for path in self.files():
            if path.stem in protected:
                continue
            if path.stat().st_mtime < cutoff:
                self._unlink(path)
                removed += 1
        return removed

    def enforce_budget(self, keep: Iterable[str] = ()) -> int:
        """Evict oldest-first until the directory fits the budget."""
        protected = self._spared(keep)
        files = sorted(self.files(), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in files)
        removed = 0

        for path in files:
            if total <= self.budget_bytes:
                break
            if path.stem in protected:
                continue
            size = path.stat().st_size
            if self._unlink(path):
                total -= size
                removed += 1

        if total > self.budget_bytes:
            logger.warning(
                "audio cache still over budget (%s > %s) after evicting %s file(s)",
                total,
                self.budget_bytes,
                removed,
            )
        return removed

    def purge(self) -> int:
        """Empty the directory. Run at startup and at shutdown so a restart
        never inherits media from the process before it."""
        removed = 0
        for path in self.files():
            if self._unlink(path):
                removed += 1
        return removed

    def _unlink(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError as exc:  # pragma: no cover - racing with another sweep
            logger.debug("could not remove %s: %s", path, exc)
            return False
