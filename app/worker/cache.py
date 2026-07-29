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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AudioCache:
    directory: Path
    budget_bytes: int
    ttl_s: int

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    # --- Lookups ---------------------------------------------------------
    def find(self, key: str) -> Path | None:
        """The cached file for a track, whatever extension it landed with."""
        for path in sorted(self.directory.glob(f"{key}.*")):
            if path.is_file():
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

    def sweep(self, keep: Iterable[str] = ()) -> int:
        """Delete anything past its TTL. ``keep`` is current + next.

        Concept §12: the current track lives until playback completes, a
        prefetched track for at most ``ttl_s``.
        """
        protected = set(keep)
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
        protected = set(keep)
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
