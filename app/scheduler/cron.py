"""Cron schedule parsing and slot arithmetic (APScheduler's CronTrigger does the calendar maths)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

MIN_INTERVAL = timedelta(minutes=15)  # profiling reads a customer warehouse; keep it polite
_SAMPLED_SLOTS = 4


def _trigger(schedule: str, timezone: str) -> CronTrigger:
    """Build a ``CronTrigger`` from a five-field cron string and zone, or raise ``ValueError``."""
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("Schedule must be a five-field cron expression")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise ValueError(f"Unknown time zone {timezone!r}") from error
    try:
        return CronTrigger.from_crontab(" ".join(fields), timezone=zone)
    except (ValueError, TypeError) as error:
        raise ValueError(f"Invalid cron expression: {error}") from error


def next_run(schedule: str, timezone: str, after: datetime) -> datetime:
    """The first slot strictly after ``after`` (timezone-aware), as an aware UTC instant."""
    if after.tzinfo is None:
        raise ValueError("'after' must be timezone-aware")
    upcoming = _trigger(schedule, timezone).get_next_fire_time(None, after + timedelta(microseconds=1))
    if upcoming is None:
        raise ValueError("Schedule never fires")
    return cast(datetime, upcoming).astimezone(UTC)


def validate_schedule(schedule: str, timezone: str, now: datetime) -> datetime:
    """Raise ValueError for a bad or too-frequent schedule; return the first slot otherwise."""
    first = next_run(schedule, timezone, now)
    slot = first
    for _ in range(_SAMPLED_SLOTS - 1):
        following = next_run(schedule, timezone, slot)
        if following - slot < MIN_INTERVAL:
            raise ValueError("Schedule fires more often than every 15 minutes")
        slot = following
    return first
