"""Case state and priority — decided by rule, never by a model.

A case is operational work, not an error row, and operational work has a state
that means something to the person holding it. Three states ("open, routed,
resolved") could not express the two distinctions that actually matter on a
finance desk:

* **We** still owe this case thought (`investigating`) versus **someone else**
  owes us an answer (`awaiting_action`). Those queues are worked by different
  people on different days, and lumping them together is why "open tickets"
  counts are ignored.
* Whether anything has been *done* yet (`detected`) versus whether it has been
  handed to an owner (`routed`).

Priority is likewise arithmetic, not judgement. An operator sorting a queue
needs the order to be stable, explicable and identical tomorrow — "the model
thought this looked urgent" is not something you can defend in a review.
"""

from __future__ import annotations

from datetime import datetime, timezone


class CaseStatus:
    #: Raised by the engine, not yet routed. A transient state in a normal run.
    DETECTED = "detected"
    #: Unnet is still working it — typically an unverified model hypothesis
    #: that a human needs to confirm or reject before anything can be asked of
    #: anyone else.
    INVESTIGATING = "investigating"
    #: Handed to an internal owner. The work is ours to do.
    ROUTED = "routed"
    #: Handed to an external party. The ball is with the bank, Razorpay support
    #: or the risk team, and chasing is the only action left to us.
    AWAITING_ACTION = "awaiting_action"
    #: Settled. Never raised again, however the underlying rows look.
    RESOLVED = "resolved"


#: The order a case moves through, used to render progress and to reject
#: nonsense transitions.
ORDER = [
    CaseStatus.DETECTED,
    CaseStatus.INVESTIGATING,
    CaseStatus.ROUTED,
    CaseStatus.AWAITING_ACTION,
    CaseStatus.RESOLVED,
]

#: Owners who are not us. A case routed to one of these is waiting on a third
#: party, and the difference is what separates "work to do" from "work to chase".
EXTERNAL_OWNERS = {"bank", "razorpay_support", "razorpay_risk"}

#: Impact classes, weighted for priority. Money a counterparty owes back is
#: worth chasing harder per rupee than a bookkeeping correction that moves no
#: money at all.
IMPACT_WEIGHT = {
    "claimable": 1.0,
    "at_risk": 0.9,
    "contestable_loss": 0.8,
    "bookkeeping": 0.3,
}


class Priority:
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


def initial_status(owner: str, has_unverified_hypothesis: bool) -> str:
    """Where a freshly-built case starts.

    Deliberately a pure function of two facts the engine already knows, so the
    same inputs always produce the same state and the rule can be read in one
    sitting.
    """
    if has_unverified_hypothesis:
        # A model has proposed something that sums but is not evidenced. Asking
        # a bank to act on an unverified guess would be worse than useless.
        return CaseStatus.INVESTIGATING
    if owner in EXTERNAL_OWNERS:
        return CaseStatus.AWAITING_ACTION
    return CaseStatus.ROUTED


def age_days(first_seen_at: datetime | None, now: datetime | None = None) -> float:
    """Days since the case was first raised.

    Tolerant of either representation on either side: SQLite returns naive
    datetimes, callers in tests pass tz-aware ones, and an exception here would
    take down a reconciliation over a formatting detail.
    """
    if first_seen_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if first_seen_at.tzinfo is None and now.tzinfo is not None:
        first_seen_at = first_seen_at.replace(tzinfo=timezone.utc)
    elif first_seen_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - first_seen_at).total_seconds() / 86400.0)


#: Anything at or above this many rupees is P1 on size alone.
P1_PAISE = 25_000_00
P2_PAISE = 2_000_00
#: A case nobody has touched for this long escalates regardless of size. Small
#: items that sit forever are how a queue rots.
STALE_DAYS = 7.0
URGENT_DAYS = 14.0


def priority(amount_paise: int, impact: str, first_seen_at: datetime | None,
             now: datetime | None = None) -> str:
    """P1/P2/P3 from money, impact class and age.

    Age escalates but never de-escalates: a large case does not become less
    urgent by being old, and a small one that has been ignored for a fortnight
    has become a process failure even if the rupees are trivial.
    """
    weighted = amount_paise * IMPACT_WEIGHT.get(impact, 0.5)
    days = age_days(first_seen_at, now)

    if weighted >= P1_PAISE or days >= URGENT_DAYS:
        return Priority.P1
    if weighted >= P2_PAISE or days >= STALE_DAYS:
        return Priority.P2
    return Priority.P3


def ageing_bucket(first_seen_at: datetime | None, now: datetime | None = None) -> str:
    """Buckets an ops lead actually reads on a morning dashboard."""
    days = age_days(first_seen_at, now)
    if days < 1:
        return "today"
    if days < 3:
        return "1-3d"
    if days < 7:
        return "3-7d"
    if days < 14:
        return "7-14d"
    return "14d+"


AGEING_BUCKETS = ["today", "1-3d", "3-7d", "7-14d", "14d+"]


def can_transition(current: str, target: str) -> bool:
    """Reject nonsense transitions.

    Resolved is terminal: a settled case does not reopen because the source
    files still contain the rows that caused it. Anything else may move freely,
    because a human reassigning work is not something to litigate.
    """
    if current == CaseStatus.RESOLVED:
        return False
    return target in ORDER
