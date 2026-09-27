"""A slot is the 15-minute bucket (UTC) a wake-up belongs to.

A re-run of the same bucket lands in the same slot, so the slot is a stable key for
idempotency (client_order_id) and for spotting gaps. GitHub starts scheduled runs 5 to
15 minutes late and sometimes drops them: a late run belongs to the bucket it starts in,
and a dropped one is recorded as missed, never caught up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import CADENCE_HOURS, SLOT_MINUTES

STEP = timedelta(minutes=SLOT_MINUTES)


def slot_for(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0)


def parse_slot(text: str) -> datetime:
    """Accept '2026-09-26T08:15Z', '2026-09-26T08:15:00+00:00' or '20260926T0815Z'."""
    t = text.strip()
    try:
        if len(t) == 14 and t.endswith("Z") and "T" in t:
            dt = datetime.strptime(t, "%Y%m%dT%H%MZ").replace(tzinfo=UTC)
        else:
            dt = datetime.fromisoformat(t)
    except ValueError as e:
        raise ValueError(f"slot {text!r} is not an ISO time") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if dt != slot_for(dt):
        raise ValueError(f"slot {text!r} is not on the schedule (every {SLOT_MINUTES} minutes, second 0)")
    return dt


def slot_id(slot: datetime) -> str:
    return slot.strftime("%Y%m%dT%H%MZ")


def previous_slot(slot: datetime) -> datetime:
    return slot_for(slot) - STEP


def missed_count(older: datetime, newer: datetime) -> int:
    """How many slots nobody ran strictly between two slots."""
    return max(0, int((newer - older) / STEP) - 1)


def cadence_bucket(slot: datetime) -> str:
    """The 6-hour bucket a slot belongs to, as a slot id: 00, 06, 12 or 18 UTC."""
    return slot_id(slot.replace(hour=slot.hour - slot.hour % CADENCE_HOURS, minute=0))


def is_last_slot_of_day(slot: datetime) -> bool:
    return (slot + STEP).date() != slot.date()
