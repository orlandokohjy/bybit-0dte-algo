"""UTC / SGT time helpers and session boundary checks."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
SGT = ZoneInfo("Asia/Singapore")


def now_utc() -> datetime:
    return datetime.now(UTC)


def today_utc() -> datetime:
    return now_utc().replace(hour=0, minute=0, second=0, microsecond=0)


def is_weekday() -> bool:
    return now_utc().weekday() < 5


def utc_time_now() -> time:
    return now_utc().time()


def next_occurrence(t: time, *, after: datetime | None = None) -> datetime:
    """Return the next datetime (UTC) when the clock hits ``t``."""
    ref = after or now_utc()
    candidate = ref.replace(
        hour=t.hour, minute=t.minute, second=t.second, microsecond=0
    )
    if candidate <= ref:
        candidate += timedelta(days=1)
    return candidate


def today_expiry_date_str() -> str:
    """
    Bybit 0DTE expiry date string for the currently active daily contract.

    Daily options listed at 08:00 UTC on day N expire at 08:00 UTC on day N+1.
    The symbol tag uses the EXPIRY date (N+1), so after 08:00 UTC we use
    tomorrow's date; before 08:00 UTC we use today's date.
    """
    d = now_utc()
    if d.hour >= 8:
        d += timedelta(days=1)
    return d.strftime("%d%b%y").upper()


def is_before_settlement(buffer_minutes: int = 5) -> bool:
    """True if we're more than ``buffer_minutes`` before 08:00 UTC settlement."""
    n = now_utc()
    settlement = n.replace(hour=8, minute=0, second=0, microsecond=0)
    if n >= settlement:
        settlement += timedelta(days=1)
    return (settlement - n).total_seconds() > buffer_minutes * 60


def seconds_until(t: time) -> float:
    """Seconds from now until the next occurrence of ``t`` (UTC)."""
    target = next_occurrence(t)
    return (target - now_utc()).total_seconds()


def format_utc_sgt(dt: datetime) -> str:
    """Format a datetime as 'HH:MM UTC / HH:MM SGT'."""
    utc_str = dt.astimezone(UTC).strftime("%H:%M UTC")
    sgt_str = dt.astimezone(SGT).strftime("%H:%M SGT")
    return f"{utc_str} / {sgt_str}"
