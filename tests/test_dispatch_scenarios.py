"""End-to-end dispatch-timing scenarios.

Covers every interaction between the queue and Claude's runtime state
that a user can observe:

  ┌──────────────────────────┬─────────────────────────────────────┐
  │ Scenario                 │ Expected behaviour                  │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ ASAP into idle Claude    │ dispatch in ≤ 1s (L1 fast path)     │
  │ ASAP into busy Claude    │ HOLD until idle, then dispatch      │
  │ ASAP, prompt regex broken│ dispatch in ~5s (L2 force fallback) │
  │ Scheduled future         │ HOLD until dispatch_at, even idle   │
  │ Scheduled past           │ dispatch like ASAP                  │
  │ 3-item burst into idle   │ dispatch in queue order, paced      │
  │ Mode toggle mid-wait     │ no breakage, eventual dispatch      │
  │ Busy regex catches all   │ no false-idle during generation     │
  │ Idle regex catches all   │ no false-busy after generation      │
  │ Recovery after slow gen  │ dispatches within 1s of idle return │
  └──────────────────────────┴─────────────────────────────────────┘
"""
from __future__ import annotations

import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import idle_detector  # noqa: E402
import monitor  # noqa: E402
import queue_store  # noqa: E402


# ============================================================
# Helpers
# ============================================================

# Canonical idle screen — has `❯` at column 1 (matches PROMPT_RE_LINE).
IDLE_TAIL = (
    "Claude's previous answer text\n"
    "more answer above\n"
    "❯\n"
)

# Canonical busy screen — claude-code 2.1.121 spinner format.
BUSY_TAIL_2_1_121 = (
    "Claude's previous answer text\n"
    "✻Manifesting…\n"
)

# 2.1.121 idle screen WITHOUT a recognizable prompt glyph (status-bar
# fragments displaced the `❯` from view) — exercises L2 fallback.
IDLE_TAIL_NO_PROMPT = (
    "Claude's previous answer text\n"
    "more answer\n"
    "answer continues\n"
    + "\n".join(str(i) for i in range(15)) + "\n"
)

# Busy screen with the spinnerless plain-text hint observed in 2.1.121.
BUSY_TAIL_PLAINTEXT = (
    "Claude's previous answer text\n"
    "almost done thinking with high effort\n"
)


def _fresh_monitor(tail_fn, write_fn, mode_fn=None,
                   poll=0.05, debounce=0.15, force=5.0,
                   commit=0.02, backoff=0.3, grace=0.1):
    rd = Path(tempfile.mkdtemp(prefix="claude_q_test_"))
    mon = monitor.Monitor(
        run_dir=rd,
        pty_tail_fn=tail_fn,
        pty_write_fn=write_fn,
        get_mode=mode_fn or (lambda: "direct"),
        poll_interval_s=poll,
        debounce_s=debounce,
        dispatch_commit_delay_s=commit,
        post_dispatch_backoff_s=backoff,
        startup_grace_s=grace,
        force_dispatch_after_stuck_s=force,
    )
    return mon, rd


def _await_dispatch(written: list, max_wait: float, target: int = 1) -> float:
    """Return seconds until `target` dispatches arrived, or max_wait+1 on timeout."""
    t0 = time.monotonic()
    deadline = t0 + max_wait
    while time.monotonic() < deadline:
        if len(written) >= target:
            return time.monotonic() - t0
        time.sleep(0.02)
    return max_wait + 1.0


def _shutdown(mon):
    mon.stop()
    time.sleep(0.2)
    for h in list(mon._logger.handlers):
        h.close()
        mon._logger.removeHandler(h)


# ============================================================
# Scenario 1: ASAP into idle Claude — must dispatch ≤ 1s
# ============================================================

def test_asap_into_idle_claude_dispatches_immediately():
    """The headline ASAP case: queue an item while Claude is idle.
    L1 fast path should fire well under 1 second."""
    written = []
    mon, rd = _fresh_monitor(
        tail_fn=lambda: IDLE_TAIL,
        write_fn=lambda b: (written.append(b) or len(b)),
    )
    queue_store.push(rd / "queue.jsonl", "asap-idle")

    mon.start()
    elapsed = _await_dispatch(written, max_wait=2.0)
    _shutdown(mon)

    assert elapsed < 1.0, (
        f"ASAP into idle should dispatch < 1s; took {elapsed:.2f}s"
    )
    assert b"asap-idle" in written[0]


# ============================================================
# Scenario 2: ASAP into busy Claude — HOLD until idle
# ============================================================

def test_asap_into_busy_claude_waits_for_completion():
    """Queue an item while Claude is mid-generation. Dispatcher must
    wait until the busy markers disappear before sending."""
    written = []
    phase = ["busy"]

    mon, rd = _fresh_monitor(
        tail_fn=lambda: BUSY_TAIL_2_1_121 if phase[0] == "busy" else IDLE_TAIL,
        write_fn=lambda b: (written.append(b) or len(b)),
    )
    queue_store.push(rd / "queue.jsonl", "patient-payload")

    mon.start()

    # Wait 1s while busy — must NOT dispatch
    time.sleep(1.0)
    assert len(written) == 0, (
        f"must not dispatch into busy Claude; got {len(written)} writes"
    )

    # Transition to idle
    phase[0] = "idle"
    elapsed_after_idle = _await_dispatch(written, max_wait=2.0)
    _shutdown(mon)

    assert len(written) == 1, "should dispatch once Claude becomes idle"
    assert elapsed_after_idle < 1.5, (
        f"after Claude went idle, dispatch should fire fast; "
        f"took {elapsed_after_idle:.2f}s"
    )
    assert b"patient-payload" in written[0]


# ============================================================
# Scenario 3: ASAP, prompt regex broken — L2 fallback ≤ 6s
# ============================================================

def test_asap_l2_fallback_when_prompt_regex_fails():
    """When 2.1.121-style status-bar fragments hide the prompt, L1
    can't fire. L2 must rescue within ~5 seconds (force threshold)."""
    written = []
    mon, rd = _fresh_monitor(
        tail_fn=lambda: IDLE_TAIL_NO_PROMPT,
        write_fn=lambda b: (written.append(b) or len(b)),
    )
    queue_store.push(rd / "queue.jsonl", "fallback-payload")

    mon.start()
    elapsed = _await_dispatch(written, max_wait=10.0)
    _shutdown(mon)

    # L1 might or might not catch it depending on tail layout; either
    # way the worst case must stay within 6s (5s force + ~1s overhead).
    assert elapsed <= 6.5, (
        f"L2 fallback should dispatch within ~5s; took {elapsed:.2f}s"
    )
    assert b"fallback-payload" in written[0]


# ============================================================
# Scenario 4: Scheduled future — HOLD until dispatch_at
# ============================================================

def test_scheduled_future_does_not_dispatch_early():
    """`dispatch_at` set 2s in the future: even with Claude fully idle,
    the item must NOT dispatch before its time."""
    from datetime import datetime, timedelta, timezone
    written = []
    mon, rd = _fresh_monitor(
        tail_fn=lambda: IDLE_TAIL,
        write_fn=lambda b: (written.append(b) or len(b)),
    )

    future = (datetime.now() + timedelta(seconds=2)).isoformat()
    queue_store.push(rd / "queue.jsonl", "future-payload",
                     dispatch_at=future)

    mon.start()

    # 0.5s in: claude is idle but item is scheduled forward → no dispatch
    time.sleep(0.5)
    assert len(written) == 0, (
        f"scheduled-future must not dispatch early; got {len(written)}"
    )

    elapsed = _await_dispatch(written, max_wait=4.0)
    _shutdown(mon)

    assert 1.5 <= elapsed <= 3.5, (
        f"scheduled-future should fire close to its dispatch_at "
        f"(~2s); fired at {elapsed:.2f}s"
    )


# ============================================================
# Scenario 5: 3-item burst — order preserved, paced by backoff
# ============================================================

def test_three_item_burst_dispatches_in_order():
    """Queue 3 items into idle Claude; they should fire in queue order.
    After each dispatch Claude transiently goes busy (real behaviour;
    needed so the `saw_busy_since_dispatch` race-guard releases for
    the next dispatch)."""
    written = []
    state = {"busy_until": 0.0}

    def tail_fn():
        # After a dispatch, Claude is busy for ~0.3s before going idle
        # again (simulating it accepting and then quick-replying).
        return BUSY_TAIL_2_1_121 if time.monotonic() < state["busy_until"] else IDLE_TAIL

    def write_fn(b):
        written.append(b)
        # Simulate Claude becoming busy on input
        state["busy_until"] = time.monotonic() + 0.3
        return len(b)

    mon, rd = _fresh_monitor(
        tail_fn=tail_fn, write_fn=write_fn, backoff=0.3,
    )
    for txt in ("first", "second", "third"):
        queue_store.push(rd / "queue.jsonl", txt)

    mon.start()
    elapsed = _await_dispatch(written, max_wait=8.0, target=3)
    _shutdown(mon)

    assert len(written) == 3, f"only {len(written)}/3 dispatched"
    payloads = [w.decode().rstrip() for w in written]
    assert payloads == ["first", "second", "third"], (
        f"queue order broken: {payloads}"
    )


# ============================================================
# Scenario 6: User in queue mode while items dispatch
#             (regression for v0.4.9 mode-gate removal)
# ============================================================

def test_dispatch_continues_while_user_sits_in_queue_mode():
    """User opens Ctrl+Q queue UI, queues 2 items, doesn't toggle
    back. Both should still dispatch — the mode gate was removed in
    v0.4.9 because it defeated the queue's whole purpose."""
    written = []
    state = {"busy_until": 0.0}

    def tail_fn():
        return BUSY_TAIL_2_1_121 if time.monotonic() < state["busy_until"] else IDLE_TAIL

    def write_fn(b):
        written.append(b)
        state["busy_until"] = time.monotonic() + 0.3
        return len(b)

    mon, rd = _fresh_monitor(
        tail_fn=tail_fn, write_fn=write_fn,
        mode_fn=lambda: "queue",  # user stays in queue mode
    )
    queue_store.push(rd / "queue.jsonl", "queued-1")
    queue_store.push(rd / "queue.jsonl", "queued-2")

    mon.start()
    elapsed = _await_dispatch(written, max_wait=5.0, target=2)
    _shutdown(mon)

    assert len(written) == 2, f"only {len(written)}/2 dispatched"


# ============================================================
# Scenario 7: Idle detector classifies known formats correctly
# ============================================================

@pytest.mark.parametrize("tail,expected_busy,name", [
    (BUSY_TAIL_2_1_121,        True,  "no-space spinner"),
    (BUSY_TAIL_PLAINTEXT,      True,  "spinnerless thinking"),
    ("✢Cogitating… 12s\n",     True,  "spinner with elapsed time"),
    ("Manifesting…\n",         True,  "verb alone with ellipsis"),
    (IDLE_TAIL,                False, "canonical idle ❯"),
    ("> answer\n❯\n",          False, "answer above prompt"),
    ("✻ Sautéed for 52s\n❯\n", False, "past-tense done"),
    ("\n",                     False, "empty screen"),
])
def test_idle_detector_busy_classification(tail, expected_busy, name):
    """The busy detector must correctly classify all known formats —
    no false-busy on idle states, no false-idle on generation."""
    s = idle_detector.IdleState()
    r = idle_detector.is_idle(tail, s, now=1.0)
    actual_busy = not r.reasons["not_busy"]
    assert actual_busy == expected_busy, (
        f"[{name}] expected busy={expected_busy}, got busy={actual_busy}; "
        f"reasons={r.reasons}"
    )


# ============================================================
# Scenario 8: Slow generation — recovery within 1s of idle return
# ============================================================

def test_recovery_within_one_second_of_idle_return():
    """Simulates a long Claude generation: queue an item while busy,
    let busy persist for 3 seconds, then transition to idle. The
    queued item must dispatch within ~1s of the idle transition."""
    written = []
    phase = ["busy"]
    mon, rd = _fresh_monitor(
        tail_fn=lambda: BUSY_TAIL_2_1_121 if phase[0] == "busy" else IDLE_TAIL,
        write_fn=lambda b: (written.append(b) or len(b)),
    )
    queue_store.push(rd / "queue.jsonl", "after-slow-gen")

    mon.start()
    time.sleep(3.0)  # busy for 3s
    assert len(written) == 0, "must hold during long generation"

    # transition
    transition_at = time.monotonic()
    phase[0] = "idle"
    elapsed = _await_dispatch(written, max_wait=2.5)
    _shutdown(mon)

    # We measure from idle transition, not from start. The dispatch
    # decision needs at least debounce_s (0.15) of new idle state to
    # be considered stable.
    real_elapsed = time.monotonic() - transition_at - 0.05
    assert real_elapsed < 1.5, (
        f"recovery after long generation should be < 1.5s after idle; "
        f"took {real_elapsed:.2f}s"
    )


# ============================================================
# Scenario 9: No dispatch during the mid-generation steady state
#             (the trickiest false-positive scenario)
# ============================================================

def test_prose_containing_arrow_glyph_is_not_mistaken_for_prompt():
    """Code-review-fixed (post-3560bd1): a Claude response that contains
    `❯` inside prose (e.g. quoting shell output, code review showing
    redirects, instructional text) must NOT be treated as a prompt
    boundary. Otherwise position-based busy scan cuts the tail at the
    prose, sees no busy markers below, and falsely reports idle while
    Claude is still generating.

    Fix: only `PROMPT_RE_LINE` (line consisting solely of the prompt
    glyph + whitespace) counts as a prompt boundary; `PROMPT_RE_END`
    (line ending with the glyph) is too lenient for this purpose.
    """
    import idle_detector

    # Claude generating a response that *contains* `❯` in prose,
    # plus an active busy marker below — must report busy.
    tail_with_prose_arrow = (
        "Sure, here's how shell redirection works:\n"
        "use the > or ❯ symbol like this:\n"   # ❯ in PROSE (end of line)
        "echo hi > /tmp/file\n"
        "and the busy spinner is still active because Claude is mid-stream\n"
        "✻Manifesting…\n"  # genuine busy marker
    )
    s = idle_detector.IdleState()
    r = idle_detector.is_idle(tail_with_prose_arrow, s, now=1.0)
    assert r.reasons["not_busy"] is False, (
        f"prose-`❯` must NOT be treated as prompt boundary; "
        f"genuine busy must still be detected. reasons={r.reasons}"
    )


def test_no_dispatch_when_busy_line_buried_far_above_status_fragments():
    """Regression v0.4.13: real-world report — Claude was responding
    (`✱ Baking…`) but our 5-line scan window saw only the status-bar
    fragments below it (token counts, elapsed time, fragments). Result:
    `not_busy=True` reported, L2 force-dispatched into the active
    generation, queued message landed mid-response.

    With _BUSY_TAIL_LINES=50, the busy line stays visible regardless
    of how many status fragments pile up below it."""
    written = []
    fragments_below = "\n".join(str(i) for i in range(40))
    busy_with_buried_marker = (
        "✱ Cogitated for 9m 16s\n"
        "> test1\n"
        "✱ Baking…\n"  # busy line — was at line -42 which used to be ignored
        + fragments_below + "\n"
    )

    mon, rd = _fresh_monitor(
        tail_fn=lambda: busy_with_buried_marker,
        write_fn=lambda b: (written.append(b) or len(b)),
        force=2.0,  # aggressive for fast test
    )
    queue_store.push(rd / "queue.jsonl", "must-not-fire")

    mon.start()
    time.sleep(4.0)  # 2× force threshold
    _shutdown(mon)

    assert len(written) == 0, (
        f"L2 must NOT fire when a busy line is anywhere in the last 50 "
        f"tail lines, even if status fragments pile up below it; "
        f"got {len(written)} writes"
    )


def test_no_dispatch_hold_when_prompt_returned_after_old_busy_marker():
    """Regression v0.4.14: real-world report — after Claude finished
    responding (`❯` visible at bottom), old `Manifesting…` lines from
    the just-completed generation were still in the PTY rolling buffer
    above the new prompt. The 50-line scan re-detected those stale
    markers as `not_busy=False`, holding the next dispatch for 38
    seconds until they rolled out of the buffer.

    Fix: only inspect lines BELOW the most recent prompt glyph for
    live busy markers. Anything above `❯` is by definition past."""
    import idle_detector

    # Realistic post-completion tail: response + transient busy lines
    # + completion + new prompt at the very bottom.
    finished_tail = (
        "Here's my answer with some text.\n"
        "and more text.\n"
        "✱ Manifesting…\n"           # stale, was a transient render
        "✱ Manifesting…\n"           # stale
        "more answer text from claude\n"
        "✱ Cogitated for 9m 16s\n"   # past tense (completion marker)
        "❯\n"                        # ← new input prompt; we're now idle
    )
    s = idle_detector.IdleState()
    r = idle_detector.is_idle(finished_tail, s, now=1.0)
    assert r.reasons["prompt_visible"] is True, (
        f"prompt should be visible; reasons={r.reasons}"
    )
    assert r.reasons["not_busy"] is True, (
        f"stale `Manifesting…` lines ABOVE the prompt must not block "
        f"dispatch; got reasons={r.reasons}"
    )


def test_extra_spinner_glyphs_caught():
    """Regression v0.4.13: claude-code 2.1.x uses additional spinner
    glyphs (✱ U+2731 heavy asterisk, ✶ U+2736 six-pointed star) that
    the v0.4.12 character set didn't include."""
    import idle_detector

    for glyph in ("✱", "✶"):
        tail = f"prior text\n{glyph} Baking…\n"
        s = idle_detector.IdleState()
        r = idle_detector.is_idle(tail, s, now=1.0)
        assert r.reasons["not_busy"] is False, (
            f"glyph {glyph!r} must be recognized as a busy spinner; "
            f"got reasons={r.reasons}"
        )


def test_no_dispatch_during_continuous_generation():
    """Claude generates for 8 seconds straight (longer than the 5s L2
    threshold). The busy regex must catch this and prevent any L2
    force-dispatch — sending mid-generation would interrupt Claude."""
    written = []
    counter = [0]

    def churning_busy_tail():
        counter[0] += 1
        # Busy marker present every tick PLUS content changes →
        # not_busy=False AND stable=False, both block dispatch.
        return f"prior text\n✻Manifesting… {counter[0]}\n"

    mon, rd = _fresh_monitor(
        tail_fn=churning_busy_tail,
        write_fn=lambda b: (written.append(b) or len(b)),
        force=2.0,  # aggressive 2s threshold to make the test fast
    )
    queue_store.push(rd / "queue.jsonl", "must-not-fire")

    mon.start()
    time.sleep(4.0)  # 2× the 2s force threshold
    _shutdown(mon)

    assert len(written) == 0, (
        f"must NOT dispatch during active generation, even past L2 "
        f"threshold; got {len(written)} writes"
    )


# ============================================================
# Scenario 10: Mode toggle mid-wait doesn't break dispatch state
# ============================================================

def test_mode_toggle_during_wait_does_not_break_dispatch():
    """User toggles between queue and direct modes while waiting for
    Claude. Dispatch state machine must remain coherent."""
    written = []
    mode = ["direct"]
    phase = ["busy"]

    mon, rd = _fresh_monitor(
        tail_fn=lambda: BUSY_TAIL_2_1_121 if phase[0] == "busy" else IDLE_TAIL,
        write_fn=lambda b: (written.append(b) or len(b)),
        mode_fn=lambda: mode[0],
    )
    queue_store.push(rd / "queue.jsonl", "toggle-survivor")

    mon.start()
    time.sleep(0.5)
    mode[0] = "queue"
    time.sleep(0.5)
    mode[0] = "direct"
    time.sleep(0.5)
    mode[0] = "queue"
    time.sleep(0.5)

    # Now transition Claude to idle
    phase[0] = "idle"
    elapsed = _await_dispatch(written, max_wait=2.0)
    _shutdown(mon)

    assert len(written) == 1
    assert b"toggle-survivor" in written[0]
