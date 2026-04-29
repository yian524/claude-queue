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
#
# We accept either:
#   a) a line ENDING with an empty prompt char (trailing whitespace OK),
#   b) a line where the prompt char is at the START and the whole line is
#      just the prompt + whitespace.
# The start-of-line form catches newer UI where Claude draws "❯ " at
# column 1 with no other content on that row.
PROMPT_RE_END = re.compile(r"(?:[❯›〉]|│\s*>\s*│?)\s*$")
PROMPT_RE_LINE = re.compile(r"^\s*[❯›〉]\s*$")

# Markers that indicate Claude is ACTIVELY working.
#
# Claude Code v2.1+ uses whimsical action verbs (Honking, Moonwalking,
# Percolating, Sautéing, Channelling, Actioning, Compacting, Thinking,
# Computing, Working, Manifesting, Cultivating, ...) with a spinner prefix
# (✻ ✢ ✽ ✺ * + ●) and an ellipsis suffix (…). When the action FINISHES,
# the same line stays on screen but uses past tense and drops the ellipsis,
# e.g. "✻ Sautéed for 52s".
#
# Across versions the renderer has flip-flopped on the space between
# spinner and verb:
#   2.0.x:   "✻ Manifesting…"   (spinner + space + verb)
#   2.1.121: "✻Manifesting…"    (spinner directly adjacent to verb)
# So `\s*` (zero-or-more) — NOT `\s+` (one-or-more) — between spinner and
# the trailing word.
#
# Reliable rule: "spinner + (optional space) + word + ellipsis" = active,
# anything else (including spinner without ellipsis like "Sautéed for N s")
# = done.
# Spinner glyph set — the leading "I'm working" indicator that Claude
# Code rotates through. Includes every glyph observed across 2.0.x and
# 2.1.x. New glyphs land here as they're spotted in the wild.
#   Plain ASCII:  + * ·
#   Circles:      ●
#   Asterisks:    ✻ U+273B  ✢ U+2722  ✽ U+273D  ✺ U+273A
#                 ✱ U+2731 (added v0.4.13 — observed as "✱ Baking…")
#                 ✶ U+2736 (added v0.4.13 — observed as "✶ Razzle-dazzling…")
BUSY_STATUS_RE = re.compile(r"^\s*[+*●·✻✢✽✺✱✶]\s*\S+…")

# Past-tense completion marker: `✱ Crunched for 4s`, `✻ Sautéed for 52s`,
# `* Cogitated for 9m 16s` — Claude prints this immediately after a
# response finishes, so it acts as a positive signal of "the most
# recent generation has ENDED". When this appears in tail BELOW any
# busy markers, the busy markers are historical residue from the
# previous generation, not the current state.
SPINNER_DONE_RE = re.compile(r"^\s*[+*●·✻✢✽✺✱✶]\s+\w+\s+for\s+\d")

# Fallback: any tail line ending in `…` is treated as busy even when no
# spinner is on screen. Newer Claude builds occasionally render plain-text
# busy hints with NO spinner glyph at all, e.g.
#   "almost done thinking with high effort"
#   "Manifesting…" (verb on its own line, spinner on previous line)
# The ellipsis is the last reliable signal common to every Claude version
# we've seen, so trust it. Excludes "..." (ASCII triple dot) to avoid
# false-matching prose like "wait...".
BUSY_ELLIPSIS_RE = re.compile(r"…\s*$")

# Literal phrase markers that always mean Claude is busy. These cover
# (a) the universal "esc to interrupt" hint Claude shows during generation,
# and (b) recent plain-text busy hints that appeared in 2.1.x WITHOUT
# a spinner glyph on the same line.
BUSY_LITERALS = (
    "esc to interrupt",
    "thinking with",       # "almost done thinking with high effort"
)

# How many trailing MEANINGFUL non-empty lines to inspect for busy
# markers. v0.4.13 widened this to 50 raw lines to defeat the status-
# bar fragments problem, but that brought back a reverse bug: after
# Claude finishes, old `Manifesting…` etc. busy lines still sit in
# the PTY tail buffer (it's a 40k-char rolling window, not a screen
# emulator) and a 50-line scan would re-detect them as "busy now".
#
# v0.4.14: scan only LINES THAT ARE MEANINGFUL (len ≥ 4 chars,
# excluding pure-digit fragments). Status-bar per-character redraws
# leave 1-3-char fragments (`1`, `82`, `p2`, `↓`) — none of which
# carry busy semantics — so they no longer count toward the window
# and 10 meaningful lines is plenty to cover the live busy marker
# while staying close enough to "right now" that we don't dredge
# stale ones from previous responses.
_BUSY_TAIL_LINES = 10
_BUSY_MIN_LINE_LEN = 4


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

    # signal 1: empty prompt in last 30 non-empty lines. Widened from 10
    # to 30 because Claude Code 2.1.121's status bar issues per-character
    # cursor-move ANSI updates that, after stripping, manifest as many
    # 1-3-char "lines" at the bottom of tail; the actual prompt glyph
    # routinely lands at line -15 to -25 in busy sessions. Without a
    # wide window the L1 fast path never sees the prompt and we fall
    # all the way to L2 force-dispatch every time.
    tail_lines_for_prompt = lines[-30:]
    has_empty_prompt = any(
        PROMPT_RE_END.search(ln) or PROMPT_RE_LINE.search(ln)
        for ln in tail_lines_for_prompt
    )

    # signal 2: no busy marker in the last few MEANINGFUL lines (filter
    # out 1-3-char status-bar fragments so they don't bury the real
    # busy line nor inflate the scan window with noise). Three
    # independent detectors — ANY hit means busy:
    #   a) spinner + verb + ellipsis  (canonical Claude busy line)
    #   b) bare line ending in `…`    (rare: spinner glyph dropped by
    #      partial PTY redraw; verb on its own line — observed in 2.1.121)
    #   c) literal phrase             ("esc to interrupt", "thinking with")
    meaningful_lines = [
        ln for ln in lines
        if len(ln.strip()) >= _BUSY_MIN_LINE_LEN
        and not ln.strip().isdigit()
    ]

    # **PROMPT-RELATIVE busy scan** — the strongest signal we have for
    # "Claude already finished and is back at the input prompt".
    #
    # When `❯` (or any prompt glyph) is visible in the tail, anything
    # ABOVE it is by definition past — Claude rendered the input
    # prompt, so the response that came before is over. Old
    # `Manifesting…` lines from the previous generation may still
    # linger in the rolling 40k PTY buffer above the new prompt, but
    # they are NOT current state. Only inspect lines BELOW the most
    # recent prompt for live busy markers.
    #
    # When no prompt is visible (the prompt is hidden during active
    # generation), fall back to scanning the last N meaningful lines
    # plus the busy/done position comparison below.
    # IMPORTANT: position-cut uses ONLY PROMPT_RE_LINE (a line that is
    # solely the prompt glyph + whitespace), not PROMPT_RE_END which
    # also matches any prose line ending with `❯` or `›`. Prose like
    # "use the ❯ symbol" or quoted shell output would otherwise be
    # mistaken for a prompt boundary, causing tail_lines below to
    # become empty and falsely report idle while Claude is still
    # generating. Code reviewer (3560bd1 review) flagged this.
    last_prompt_idx_in_meaningful = -1
    for i in range(len(meaningful_lines) - 1, -1, -1):
        ln = meaningful_lines[i]
        if PROMPT_RE_LINE.search(ln):
            last_prompt_idx_in_meaningful = i
            break

    if last_prompt_idx_in_meaningful >= 0:
        # Claude is at an input prompt — only "now" is below it.
        tail_lines = meaningful_lines[last_prompt_idx_in_meaningful + 1:]
    else:
        tail_lines = meaningful_lines[-_BUSY_TAIL_LINES:]

    # Compare positions: if a "completion" marker (e.g. "Crunched for 4s")
    # appears AFTER any busy markers in the tail, the busy markers are
    # historical residue from the previous generation — Claude has
    # finished and is now idle. This is the secondary safety check
    # for the "no-prompt-visible" branch above.
    busy_idx = -1
    done_idx = -1
    for i, ln in enumerate(tail_lines):
        if (BUSY_STATUS_RE.search(ln) or BUSY_ELLIPSIS_RE.search(ln)
                or any(lit in ln for lit in BUSY_LITERALS)):
            busy_idx = i
        if SPINNER_DONE_RE.search(ln):
            done_idx = i

    if busy_idx == -1:
        busy = False
    elif done_idx > busy_idx:
        # Completion marker is more recent than any busy marker → idle.
        busy = False
    else:
        busy = True

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

# Real captures from Claude Code 2.1.121 PTY tail. These are the formats
# that broke the v0.4.6 regex (no space between spinner and verb).
_FAKE_BUSY_NOSPACE = """
Some prior answer text.
✻Manifesting…
"""

_FAKE_BUSY_SPINNERLESS = """
Some prior answer text.
almost done thinking with high effort
"""

# Verb on its own line WITHOUT a spinner glyph (observed when partial
# PTY redraws split the busy hint across two lines).
_FAKE_BUSY_VERB_ALONE = """
Some prior answer text.
Manifesting…
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

    # 2.1.121 regression: `✻Manifesting…` (no space) must mark busy.
    s_no_space = IdleState()
    r_ns = is_idle(_FAKE_BUSY_NOSPACE, s_no_space, now=0.0, debounce_s=0.6)
    assert r_ns.reasons["not_busy"] is False, \
        f"`✻Manifesting…` should mark busy; got {r_ns.reasons}"
    assert r_ns.idle is False

    # 2.1.121 regression: spinner-less hint `almost done thinking with high effort`
    s_no_spinner = IdleState()
    r_nsp = is_idle(_FAKE_BUSY_SPINNERLESS, s_no_spinner, now=0.0, debounce_s=0.6)
    assert r_nsp.reasons["not_busy"] is False, \
        f"`thinking with high effort` should mark busy; got {r_nsp.reasons}"

    # 2.1.121 regression: verb on its own line, ending in ellipsis
    s_verb_alone = IdleState()
    r_va = is_idle(_FAKE_BUSY_VERB_ALONE, s_verb_alone, now=0.0, debounce_s=0.6)
    assert r_va.reasons["not_busy"] is False, \
        f"bare `Manifesting…` should mark busy; got {r_va.reasons}"

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
