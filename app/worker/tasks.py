"""One task per key, shared by everyone who asks for it.

Downloading a track and measuring its loudness are the same problem twice: the
lookahead starts one early, the playback loop asks for the same thing at the
boundary if the lookahead did not get there first, and doing it twice is either
a corrupted file or a wasted ffmpeg. Both also have callers that are routinely
cancelled -- a skip, a lost room lock, the end of the track being prepared for
-- and abandoning work that is nearly done is how a boundary pays for it twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine


def retrieve_failure(task: asyncio.Task) -> None:
    """Work whose only caller went away still has to have its exception looked
    at, or asyncio complains when the task is collected."""
    if not task.cancelled():
        task.exception()


class SharedTasks[T]:
    """A keyed registry of work in flight. Second caller waits on the first."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[T]] = {}

    @property
    def pending(self) -> set[str]:
        return {key for key, task in self._tasks.items() if not task.done()}

    async def run(self, key: str, factory: Callable[[], Coroutine[object, object, T]]) -> T:
        """``factory()``'s result, starting it only if nobody else already has."""
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(factory())
            task.add_done_callback(retrieve_failure)
            # Done tasks are dropped here rather than on completion: a callback
            # cannot know whether somebody is still about to await the result.
            self._tasks = {other: t for other, t in self._tasks.items() if not t.done()}
            self._tasks[key] = task

        # Shielded on purpose -- see the module docstring.
        return await asyncio.shield(task)

    def cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def abandon(self) -> None:
        """Let go of everything still in flight."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
