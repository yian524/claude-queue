"""idle_detector.py — decide if the claude PTY is idle enough to inject the next prompt.

Three-signal AND (composite) design
-----------------------------------
idle := has_empty_prompt AND not_busy AND content_stable_for_debounce_s

signal 1: PROMPT_RE matches on any of last 5 non-empty lines of stripped output
signal 2: no BUSY_MARKER in last 10 lines of stripped output
signal 3: md5(stripped_tail) is unchanged for >= debounce_s

Degradation
-----------
If PROMPT_RE hasn't matched for `prompt_no_match_warn_s` seconds but signals
2 + 3 still hold for that duration, we flag `drift_detected=True` so the
status_bar can warn the user and the monitor can optionally degrade to
time-only idle detection.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Claude Code prompt styles across versions:
#   v1: │ >     │   (boxed)
#   v2: ❯       (heavy angle, 2.1.x)
#   v2: ›       (single angle)
#   fallback > at end of line
# Any of these followed only by whitespace means the input area is empty.
PROMPT_RE = re.compile(r"(?:[❯›〉]|│\s*>\s*│?)\s*$")

# Markers that indicate Claude is ACTIVELY working.
#
# Claude Code v2.1+ uses whimsical action verbs (Honking, Moonwalking,
# Percolating, Sautéing, Channelling, Actioning, Compacting, Thinking,
# Computing, Working) with a spinner prefix (✻ ✢ ✽ ✺ * + ●) and an ellipsis
# suffix (…). When the action FINISHES, the same line stays on screen but
# uses past tense and drops the ellipsis, e.g. "✻ Sautéed for 52s".
#
# So the reliable rule is: "spinner + ellipsis" = active, anything else
# (including spinner without ellipsis, like "Sautéed for N s") = done.
#
# We also always treat "esc to interrupt" as busy — Claude only shows that
# hint while genuinely mid-generation.
BUSY_STATUS_RE = re.compile(r"^\s*[+*●·✻✢✽✺]\s+\S+…")
BUSY_LITERALS = ("esc to interrupt",)

# How many trailing non-empty lines to inspect for busy markers.
# Busy indicators are always on the bottom status line(s); older scrollback
# should not count.
_BUSY_TAIL_LINES = 3


@dataclass
class IdleState:
    """Rolling state carried across calls by the monitor loop."""
    prev_hash: str = ""
    stable_since: float = 0.0
    last_prompt_match_at: float = 0.0
    drift_detected: bool = False


@dataclass
class IdleResult:
    idle: bool
    hash: str
    reasons: Dict[str, bool] = field(default_factory=dict)
    drift_detected: bool = False
    stable_since: float = 0.0
    last_prompt_match_at: float = 0.0


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def is_idle(
    tail_output: str,
    state: IdleState,
    now: float,
    debounce_s: float = 0.6,
    prompt_no_match_warn_s: float = 30.0,
) -> IdleResult:
    """Pure function: given the latest PTY tail and current state, decide idle.

    Updates are returned in IdleResult (caller writes them back to state).
    """
    clean = _strip_ansi(tail_output)
    lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]

    # signal 1: empty prompt in last 5 non-empty lines
    has_empty_prompt = any(PROMPT_RE.search(ln) for ln in lines[-5:])

    # signal 2: no busy marker in the LAST FEW lines (not whole tail),
    # so stale indicators from earlier answers don't keep us stuck.
    tail_lines = lines[-_BUSY_TAIL_LINES:]
    busy = (
        any(BUSY_STATUS_RE.search(ln) for ln in tail_lines)
        or any(lit in ln for ln in tail_lines for lit in BUSY_LITERALS)
    )

    # signal 3: content stable for debounce_s
    h = hashlib.md5(clean.encode("utf-8", errors="replace")).hexdigest()
    stable_since = state.stable_since if h == state.prev_hash else now
    stable = (now - stable_since) >= debounce_s

    # drift detection
    last_prompt_match_at = now if has_empty_prompt else state.last_prompt_match_at
    drift = False
    if last_prompt_match_at > 0:
        time_since_prompt = now - last_prompt_match_at
        drift = time_since_prompt >= prompt_no_match_warn_s and not busy

    idle = has_empty_prompt and not busy and stable
    return IdleResult(
        idle=idle,
        hash=h,
        reasons={
            "prompt_visible": has_empty_prompt,
            "not_busy": not busy,
            "stable": stable,
        },
        drift_detected=drift,
        stable_since=stable_since,
        last_prompt_match_at=last_prompt_match_at,
    )


def apply_result(state: IdleState, r: IdleResult) -> None:
    """Copy transient fields from IdleResult back to rolling state."""
    state.prev_hash = r.hash
    state.stable_since = r.stable_since
    state.last_prompt_match_at = r.last_prompt_match_at
    state.drift_detected = r.drift_detected


# ------------------------------- self-test -------------------------------

_FAKE_IDLE = """
Here is the answer you asked for.
More answer text here.
✻ Sautéed for 52s
❯
"""

_FAKE_STREAMING = """
I'll think about this carefully.

✢ Channelling… (7s · ↓ 259 tokens)
"""

_FAKE_THINKING = """
The refactor should touch three files.
Let me draft the change.
✻  esc to interrupt
"""


def _self_test() -> int:
    # IDLE fixture → should eventually become idle after debounce
    s = IdleState()

    r1 = is_idle(_FAKE_IDLE, s, now=0.0, debounce_s=0.6)
    assert r1.reasons["prompt_visible"], f"idle fixture should match PROMPT_RE; {r1.reasons}"
    assert r1.reasons["not_busy"], f"idle fixture should not be busy; {r1.reasons}"
    # first call: stable_since just got set to now, debounce not yet met
    assert r1.reasons["stable"] is False
    assert r1.idle is False
    apply_result(s, r1)

    # second call 0.3s later, same hash -> still not stable enough
    r2 = is_idle(_FAKE_IDLE, s, now=0.3, debounce_s=0.6)
    assert r2.idle is False
    apply_result(s, r2)

    # third call 0.7s later, same hash -> stable, idle
    r3 = is_idle(_FAKE_IDLE, s, now=0.7, debounce_s=0.6)
    assert r3.idle is True, f"should be idle after debounce; {r3.reasons}"

    # STREAMING fixture (busy marker Tokens:) must never report idle
    s2 = IdleState()
    for t in (0.0, 0.7, 1.5, 3.0):
        r = is_idle(_FAKE_STREAMING, s2, now=t, debounce_s=0.6)
        assert r.reasons["not_busy"] is False, "streaming: Tokens: should mark busy"
        assert r.idle is False
        apply_result(s2, r)

    # THINKING fixture (✻ + esc to interrupt) must also never be idle
    s3 = IdleState()
    for t in (0.0, 0.7, 1.5):
        r = is_idle(_FAKE_THINKING, s3, now=t, debounce_s=0.6)
        assert r.reasons["not_busy"] is False, "thinking: ✻ or esc should mark busy"
        assert r.idle is False
        apply_result(s3, r)

    # drift: push a fixture without PROMPT_RE and without busy; 31 seconds later → drift
    drift_fixture = "plain text output\nno prompt here\nno busy markers\n"
    s4 = IdleState()
    # baseline: saw a prompt at t=1.0
    r = is_idle(_FAKE_IDLE, s4, now=1.0)
    apply_result(s4, r)
    assert s4.last_prompt_match_at == 1.0
    # 32s later only see drift_fixture (no prompt, no busy) -> 31s without prompt
    r_later = is_idle(drift_fixture, s4, now=32.0)
    assert r_later.drift_detected is True, f"31s without prompt + not busy → drift; got {r_later}"

    print("idle_detector.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
