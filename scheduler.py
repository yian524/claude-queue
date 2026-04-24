"""scheduler.py - parse human-friendly duration/time strings for /wait and /at.

Accepted inputs
---------------

Duration (for /wait):
    30s          -> 30 seconds
    5m           -> 5 minutes
    1h           -> 1 hour
    1h30m        -> 1 hour 30 minutes
    2h15m30s     -> 2 hours 15 min 30 sec
    90s          -> 90 seconds (allowed)

Absolute time (for /at):
    14:30            -> today 14:30 (if already past, tomorrow 14:30)
    14:30:00         -> same, with seconds
    2026-04-25 14:30 -> specific date+time
    2026-04-25T14:30 -> ISO-ish form also accepted

All outputs are ISO-8601 strings without timezone (local time).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional


_DURATION_RE = re.compile(
    r"^\s*(?:(?P<h>\d+)h)?\s*(?:(?P<m>\d+)m)?\s*(?:(?P<s>\d+)s)?\s*$",
    re.IGNORECASE,
)


class ScheduleParseError(ValueError):
    """Raised when a user-supplied duration or time string can't be parsed."""


def parse_duration(text: str) -> timedelta:
    """Parse '5m', '1h30m', '90s', '2h15m30s'. At least one unit required."""
    if not text or not text.strip():
        raise ScheduleParseError("empty duration")
    m = _DURATION_RE.match(text.strip())
    if not m or not any(m.groupdict().values()):
        raise ScheduleParseError(
            f"invalid duration {text!r}; use forms like 30s, 5m, 1h, 1h30m, 2h15m30s"
        )
    h = int(m.group("h") or 0)
    mm = int(m.group("m") or 0)
    ss = int(m.group("s") or 0)
    total = h * 3600 + mm * 60 + ss
    if total <= 0:
        raise ScheduleParseError("duration must be > 0")
    return timedelta(seconds=total)


def parse_absolute_time(text: str, now: Optional[datetime] = None) -> datetime:
    """Parse '14:30', '14:30:00', '2026-04-25 14:30', '2026-04-25T14:30'.

    For bare time (HH:MM), resolve to today; if already past, resolve to
    tomorrow (common intuitive "set-an-alarm" semantics).
    """
    if not text or not text.strip():
        raise ScheduleParseError("empty time")
    text = text.strip()
    now = now or datetime.now()

    # full ISO with T or space separator
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    # bare time HH:MM or HH:MM:SS -> today (or tomorrow if past)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt).time()
            candidate = now.replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        except ValueError:
            continue

    raise ScheduleParseError(
        f"invalid time {text!r}; use HH:MM, HH:MM:SS, or YYYY-MM-DD HH:MM"
    )


def dispatch_at_from_wait(duration_text: str) -> str:
    """/wait helper: returns ISO-8601 timestamp for now + duration."""
    delta = parse_duration(duration_text)
    return (datetime.now() + delta).isoformat(timespec="seconds")


def dispatch_at_from_at(time_text: str) -> str:
    """/at helper: returns ISO-8601 timestamp for the absolute time."""
    return parse_absolute_time(time_text).isoformat(timespec="seconds")


def humanize_delta(iso_ts: str, now: Optional[datetime] = None) -> str:
    """Format 'how long from now' for UI display.

    Examples: 'in 3m 12s', 'in 2h', 'overdue 1m', 'now'.
    """
    now = now or datetime.now()
    try:
        target = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return iso_ts  # fallback
    delta = target - now
    secs = int(delta.total_seconds())
    overdue = secs < 0
    secs = abs(secs)
    if secs < 60:
        txt = f"{secs}s"
    elif secs < 3600:
        txt = f"{secs // 60}m {secs % 60}s"
    elif secs < 86400:
        h, rem = divmod(secs, 3600)
        txt = f"{h}h {rem // 60}m"
    else:
        d, rem = divmod(secs, 86400)
        h = rem // 3600
        txt = f"{d}d {h}h"
    return ("overdue " + txt) if overdue else ("in " + txt)


# ------------------------------- self-test -------------------------------

def _self_test() -> int:
    # duration parsing
    assert parse_duration("30s").total_seconds() == 30
    assert parse_duration("5m").total_seconds() == 300
    assert parse_duration("1h").total_seconds() == 3600
    assert parse_duration("1h30m").total_seconds() == 5400
    assert parse_duration("2h15m30s").total_seconds() == 8130
    assert parse_duration(" 1h   30m ").total_seconds() == 5400
    try:
        parse_duration("")
        raise AssertionError("empty should raise")
    except ScheduleParseError:
        pass
    try:
        parse_duration("xyz")
        raise AssertionError("garbage should raise")
    except ScheduleParseError:
        pass

    # absolute time parsing
    now = datetime(2026, 4, 24, 10, 0, 0)
    # future time today
    t = parse_absolute_time("14:30", now=now)
    assert t == datetime(2026, 4, 24, 14, 30, 0), t
    # past time -> tomorrow
    t = parse_absolute_time("09:00", now=now)
    assert t == datetime(2026, 4, 25, 9, 0, 0), t
    # full ISO
    t = parse_absolute_time("2026-05-01 18:00", now=now)
    assert t == datetime(2026, 5, 1, 18, 0, 0)
    t = parse_absolute_time("2026-05-01T18:00:30", now=now)
    assert t == datetime(2026, 5, 1, 18, 0, 30)

    # humanize
    future = (datetime.now() + timedelta(seconds=200)).isoformat(timespec="seconds")
    h = humanize_delta(future)
    assert h.startswith("in "), h

    past = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    assert humanize_delta(past).startswith("overdue "), humanize_delta(past)

    print("scheduler.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
