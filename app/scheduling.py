"""Queue ordering — fairness first, popularity second (concept §8).

Pure functions over plain values: no database, no clock, no I/O. The API sorts
the queue it renders with them and the worker picks the next track with them,
so what a listener sees at the top is what plays next.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candidate:
    """The only things ordering depends on."""

    id: str
    added_by: str
    score: int
    created_at: datetime
    # Host override. Non-zero jumps the fairness rules entirely — that is what
    # "reorder queue" means for a host, and only a host can set it.
    priority: int = 0


def _own_order(candidate: Candidate) -> tuple:
    """Within one submitter: their best-scoring track goes first, ties broken
    by submission time so the order is stable while people vote."""
    return (-candidate.priority, -candidate.score, candidate.created_at, candidate.id)


def order_queue(candidates: list[Candidate]) -> list[Candidate]:
    """Round-robin across submitters.

    Rule 1 — no user may dominate the queue: everyone's first track plays
    before anyone's second, regardless of how many each has queued.

    Rule 2 — fairness over popularity: score only ever decides *within* a
    round, never across rounds, so twenty upvotes cannot buy a second turn.

        Alice -> A1     (round 0)
        Bob   -> B1     (round 0)
        Alice -> A2     (round 1)
        Carl  -> C1     ... Carl joined late: he still lands in round 0

    Host-promoted tracks sit above all of it, newest promotion first.
    """
    by_submitter: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_submitter[candidate.added_by].append(candidate)

    ranked: list[tuple[int, Candidate]] = []
    for submissions in by_submitter.values():
        for round_index, candidate in enumerate(sorted(submissions, key=_own_order)):
            ranked.append((round_index, candidate))

    ranked.sort(
        key=lambda pair: (
            -pair[1].priority,
            pair[0],
            -pair[1].score,
            pair[1].created_at,
            pair[1].id,
        )
    )
    return [candidate for _, candidate in ranked]
