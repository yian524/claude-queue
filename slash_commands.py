"""slash_commands.py - parse and execute /slash commands in queue input.

Commands
--------

    /wait <duration> <message>
        Queue <message> and dispatch it only after <duration> has elapsed
        AND Claude is idle. Example: /wait 5m please run the tests

    /at <time> <message>
        Queue <message> for dispatch at absolute <time>. Time accepts
        HH:MM, HH:MM:SS, or YYYY-MM-DD HH:MM. Example: /at 14:30 deploy

    /priority <message>
        Queue <message> with priority=100 so it's dispatched before any
        normal-priority entry regardless of order.

    /now <message>
        (DANGEROUS) Dispatch <message> immediately, bypassing the idle
        wait. Will interrupt Claude's current output.

    /cancel
        Exit queue mode without queuing anything.

    /help
        Show the command list (handled by caller via `HelpRequest`).

The parse() function returns one of several Result dataclasses so the
caller can dispatch to the right side-effect.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional, Union

import scheduler


# ============================= result types =============================

@dataclass
class QueueRequest:
    """Plain queue: dispatch when Claude is idle."""
    text: str
    dispatch_at: Optional[str] = None   # ISO string, None = ASAP
    priority: int = 0


@dataclass
class ForceSendRequest:
    """/now - skip idle check, write directly to PTY."""
    text: str


@dataclass
class CancelRequest:
    """/cancel - exit queue mode without pushing anything."""


@dataclass
class HelpRequest:
    """/help - show command reference."""


@dataclass
class DropRequest:
    """/drop <N> - drop pending entry at 1-based index N."""
    index: int


@dataclass
class ClearRequest:
    """/clear - drop all pending entries."""


@dataclass
class ParseError:
    """Malformed command; caller should show the message and stay in queue."""
    message: str


ParseResult = Union[
    QueueRequest, ForceSendRequest, CancelRequest, HelpRequest,
    DropRequest, ClearRequest, ParseError,
]


# ============================= command metadata =============================

# Shown to dropdown UI; order matters for display.
# /cancel is intentionally omitted: Esc and Ctrl+Q already cancel.
COMMANDS: list[dict] = [
    {
        "name": "/wait",
        "template": "/wait <duration> <message>",
        "summary": "Dispatch after duration (30s, 5m, 1h30m)",
    },
    {
        "name": "/at",
        "template": "/at <time> <message>",
        "summary": "Dispatch at absolute time (HH:MM or YYYY-MM-DD HH:MM)",
    },
    {
        "name": "/priority",
        "template": "/priority <message>",
        "summary": "Jump ahead of normal queue entries",
    },
    {
        "name": "/now",
        "template": "/now <message>",
        "summary": "WARNING: send immediately, interrupts Claude",
    },
    {
        "name": "/drop",
        "template": "/drop <N>",
        "summary": "Drop pending entry #N (see the Pending list)",
    },
    {
        "name": "/clear",
        "template": "/clear",
        "summary": "Drop ALL pending entries",
    },
    {
        "name": "/help",
        "template": "/help",
        "summary": "Show this command list",
    },
]


def filter_commands(prefix: str) -> list[dict]:
    """Return commands whose name starts with `prefix` (case-insensitive)."""
    if not prefix:
        return list(COMMANDS)
    p = prefix.lower()
    return [c for c in COMMANDS if c["name"].lower().startswith(p)]


# ============================= parser =============================

def parse(raw: str) -> ParseResult:
    """Parse the user's raw input line (after Enter).

    If the first token is not a /command, returns a plain QueueRequest
    with the original text. Otherwise dispatches to the right result.
    """
    if not raw or not raw.strip():
        return ParseError("empty input; type something or Esc to cancel")

    stripped = raw.strip()
    if not stripped.startswith("/"):
        return QueueRequest(text=stripped)

    # split into /cmd + rest
    try:
        parts = stripped.split(None, 1)
    except ValueError:
        return ParseError("malformed command")
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HelpRequest()
    if cmd == "/priority":
        if not rest.strip():
            return ParseError("/priority needs a message")
        return QueueRequest(text=rest.strip(), priority=100)
    if cmd == "/now":
        if not rest.strip():
            return ParseError("/now needs a message")
        return ForceSendRequest(text=rest.strip())
    if cmd == "/wait":
        return _parse_wait(rest)
    if cmd == "/at":
        return _parse_at(rest)
    if cmd == "/drop":
        return _parse_drop(rest)
    if cmd == "/clear":
        return ClearRequest()

    return ParseError(f"unknown command {cmd!r}; try /help")


def _parse_drop(rest: str) -> ParseResult:
    if not rest.strip():
        return ParseError("/drop needs a pending index (e.g. /drop 1)")
    try:
        idx = int(rest.strip())
    except ValueError:
        return ParseError(f"/drop: {rest.strip()!r} is not a valid index")
    if idx < 1:
        return ParseError("/drop: index must be >= 1")
    return DropRequest(index=idx)


def _parse_wait(rest: str) -> ParseResult:
    """Parse '<duration> <message>' for /wait."""
    if not rest.strip():
        return ParseError("/wait needs a duration and a message")
    try:
        dur, msg = rest.split(None, 1)
    except ValueError:
        return ParseError("/wait needs a message after the duration")
    if not msg.strip():
        return ParseError("/wait needs a message after the duration")
    try:
        dispatch_at = scheduler.dispatch_at_from_wait(dur)
    except scheduler.ScheduleParseError as e:
        return ParseError(str(e))
    return QueueRequest(text=msg.strip(), dispatch_at=dispatch_at)


def _parse_at(rest: str) -> ParseResult:
    """Parse '<time> <message>' for /at.

    Time may contain a space (YYYY-MM-DD HH:MM) so we can't naively split.
    Strategy: try increasing split points; first one that parses as time wins.
    """
    if not rest.strip():
        return ParseError("/at needs a time and a message")
    tokens = rest.split()
    # try absorbing 1..n tokens as the time, rest as message
    for cut in range(1, len(tokens)):
        time_str = " ".join(tokens[:cut])
        msg = " ".join(tokens[cut:]).strip()
        if not msg:
            continue
        try:
            dispatch_at = scheduler.dispatch_at_from_at(time_str)
            return QueueRequest(text=msg, dispatch_at=dispatch_at)
        except scheduler.ScheduleParseError:
            continue
    return ParseError("/at: could not parse time. "
                      "Try /at 14:30 <msg> or /at 2026-04-25 14:30 <msg>")


# ============================= self-test =============================

def _self_test() -> int:
    # plain text
    r = parse("hello world")
    assert isinstance(r, QueueRequest) and r.text == "hello world"
    assert r.dispatch_at is None and r.priority == 0

    # /help (dropdown-visible)
    assert isinstance(parse("/help"), HelpRequest)
    # /cancel was removed in v0.3.1 — Esc / Ctrl+Q handle cancellation
    assert isinstance(parse("/cancel"), ParseError)

    # /priority
    r = parse("/priority pick me first")
    assert isinstance(r, QueueRequest) and r.text == "pick me first" and r.priority == 100
    assert isinstance(parse("/priority"), ParseError)

    # /now
    r = parse("/now urgent")
    assert isinstance(r, ForceSendRequest) and r.text == "urgent"
    assert isinstance(parse("/now"), ParseError)

    # /wait
    r = parse("/wait 5m do the thing")
    assert isinstance(r, QueueRequest) and r.text == "do the thing"
    assert r.dispatch_at is not None and len(r.dispatch_at) >= 19
    assert isinstance(parse("/wait"), ParseError)
    assert isinstance(parse("/wait garbage hello"), ParseError)
    assert isinstance(parse("/wait 5m"), ParseError)  # no message

    # /at HH:MM
    r = parse("/at 23:59 end of day")
    assert isinstance(r, QueueRequest) and r.text == "end of day"
    assert r.dispatch_at is not None

    # /at YYYY-MM-DD HH:MM (space inside time)
    r = parse("/at 2099-01-01 00:00 new year")
    assert isinstance(r, QueueRequest) and r.text == "new year", r

    # /at bad
    assert isinstance(parse("/at"), ParseError)
    assert isinstance(parse("/at 25:99 msg"), ParseError)  # invalid time
    assert isinstance(parse("/at 14:30"), ParseError)  # no message

    # unknown command
    assert isinstance(parse("/foobar"), ParseError)

    # /drop
    r = parse("/drop 2")
    assert isinstance(r, DropRequest) and r.index == 2
    assert isinstance(parse("/drop"), ParseError)
    assert isinstance(parse("/drop abc"), ParseError)
    assert isinstance(parse("/drop 0"), ParseError)

    # /clear
    assert isinstance(parse("/clear"), ClearRequest)

    # empty
    assert isinstance(parse(""), ParseError)
    assert isinstance(parse("   "), ParseError)

    # filter_commands
    assert len(filter_commands("")) == len(COMMANDS)
    assert len(filter_commands("/w")) == 1
    assert filter_commands("/w")[0]["name"] == "/wait"
    assert len(filter_commands("/p")) == 1
    assert filter_commands("/p")[0]["name"] == "/priority"
    assert len(filter_commands("/xyz")) == 0

    print("slash_commands.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
