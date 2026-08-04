"""Fair scheduling (concept §8) — the rule the whole queue hangs on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.scheduling import Candidate, order_queue

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(name: str, who: str, minutes: int, score: int = 0, priority: int = 0) -> Candidate:
    return Candidate(
        id=name,
        added_by=who,
        score=score,
        created_at=BASE + timedelta(minutes=minutes),
        priority=priority,
    )


def ids(candidates: list[Candidate]) -> list[str]:
    return [item.id for item in candidates]


def test_empty_queue_has_no_next():
    assert order_queue([]) == []


def test_round_robin_interleaves_submitters():
    """The example from the design document, verbatim."""
    queue = [
        candidate("A1", "alice", 0),
        candidate("A2", "alice", 1),
        candidate("B1", "bob", 2),
        candidate("C1", "carl", 3),
    ]
    assert ids(order_queue(queue)) == ["A1", "B1", "C1", "A2"]


def test_one_user_cannot_dominate():
    """Twenty submissions from one person do not become twenty slots."""
    queue = [candidate(f"A{i}", "alice", i) for i in range(20)]
    queue.append(candidate("B1", "bob", 99))

    order = ids(order_queue(queue))
    assert order[:2] == ["A0", "B1"]
    assert order[2] == "A1"


def test_score_orders_within_a_round():
    queue = [
        candidate("A1", "alice", 0, score=0),
        candidate("B1", "bob", 1, score=5),
        candidate("C1", "carl", 2, score=-2),
    ]
    assert ids(order_queue(queue)) == ["B1", "A1", "C1"]


def test_score_cannot_buy_a_second_turn():
    """Fairness over popularity: votes choose *which* of Alice's tracks plays
    in her slot, never how many slots she gets."""
    queue = [
        candidate("A1", "alice", 0, score=0),
        candidate("A2", "alice", 1, score=100),
        candidate("B1", "bob", 2, score=0),
    ]
    order = ids(order_queue(queue))

    # A2 wins Alice's turn on score, and Bob still plays before she gets another.
    assert order == ["A2", "B1", "A1"]


def test_host_priority_beats_fairness():
    queue = [
        candidate("A1", "alice", 0, score=10),
        candidate("B1", "bob", 1),
        candidate("B2", "bob", 2, priority=1),
    ]
    assert ids(order_queue(queue))[0] == "B2"


def test_ordering_is_stable_for_identical_input():
    queue = [
        candidate("A1", "alice", 0),
        candidate("B1", "bob", 0),
        candidate("C1", "carl", 0),
    ]
    assert ids(order_queue(queue)) == ids(order_queue(list(reversed(queue))))
