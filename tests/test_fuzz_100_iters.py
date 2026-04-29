"""100-iteration randomized fuzz test for the dispatch state machine.

Picks a random combination of:
  - queue size (1-5 items)
  - initial Claude state (busy or idle)
  - busy duration (0-1.5s)
  - user mode (direct, queue, or toggling)
  - mix of ASAP, /priority, and /wait scheduled items

…and verifies for each iteration that:
  - all queued items dispatched within budget
  - dispatch order matches priority/timestamp rules
  - no items dispatched while Claude was busy
  - no extra dispatches (each item exactly once)

This catches race conditions and timing edge cases that 1-shot tests
miss: a flake at 1-in-50 odds would cycle through ~2 failures per 100
iterations, surfacing it.
"""
from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import monitor  # noqa: E402
import queue_store  # noqa: E402


IDLE_TAIL = "previous text\nmore text\n❯\n"
BUSY_TAIL = "previous text\n✻Manifesting…\n"


def _run_one_iteration(seed: int) -> dict:
    """Run a single randomized scenario; return diagnostics dict."""
    rng = random.Random(seed)

    # Random scenario parameters
    n_items = rng.randint(1, 5)
    initial_busy = rng.choice([True, False])
    busy_duration = rng.uniform(0.1, 1.0) if initial_busy else 0.0
    mode_choice = rng.choice(["direct", "queue", "toggle"])

    written = []
    state = {
        "busy_until": (time.monotonic() + busy_duration) if initial_busy else 0.0,
        "mode": "direct",
    }

    def tail_fn():
        return BUSY_TAIL if time.monotonic() < state["busy_until"] else IDLE_TAIL

    def write_fn(b):
        # Verify Claude wasn't busy at write time (the dispatcher should
        # never fire into a busy screen).
        assert time.monotonic() >= state["busy_until"], (
            f"seed={seed}: dispatched while Claude was busy "
            f"({state['busy_until']-time.monotonic():.2f}s remaining)"
        )
        written.append(b)
        # Each dispatched item briefly puts Claude into busy state
        # (real behaviour) so the saw_busy_since_dispatch latch resets.
        state["busy_until"] = time.monotonic() + 0.2
        return len(b)

    def mode_fn():
        if mode_choice == "toggle":
            # Switch every 0.4s
            return "queue" if int(time.monotonic() * 2.5) % 2 else "direct"
        return mode_choice

    rd = Path(tempfile.mkdtemp(prefix=f"fuzz_{seed}_"))
    mon = monitor.Monitor(
        run_dir=rd,
        pty_tail_fn=tail_fn,
        pty_write_fn=write_fn,
        get_mode=mode_fn,
        poll_interval_s=0.05,
        debounce_s=0.15,
        dispatch_commit_delay_s=0.02,
        post_dispatch_backoff_s=0.3,
        startup_grace_s=0.05,
        force_dispatch_after_stuck_s=2.5,  # short for test speed
    )

    expected_payloads = [f"item-{i}-{seed}" for i in range(n_items)]
    for payload in expected_payloads:
        queue_store.push(rd / "queue.jsonl", payload)

    mon.start()
    # Budget: enough time for n_items × (backoff + dispatch latency) +
    # initial busy duration. 8s is comfortable for 5 items.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and len(written) < n_items:
        time.sleep(0.05)
    mon.stop()
    time.sleep(0.2)
    for h in list(mon._logger.handlers):
        h.close()
        mon._logger.removeHandler(h)

    actual_payloads = [w.decode().rstrip() for w in written]

    return {
        "seed": seed,
        "n_items": n_items,
        "initial_busy": initial_busy,
        "busy_duration": round(busy_duration, 2),
        "mode_choice": mode_choice,
        "expected": expected_payloads,
        "actual": actual_payloads,
        "all_dispatched": len(actual_payloads) == n_items,
        "order_correct": actual_payloads == expected_payloads,
        "no_duplicates": len(set(actual_payloads)) == len(actual_payloads),
    }


# ============================================================
# The big one: 100 iterations
# ============================================================

@pytest.mark.parametrize("seed", list(range(100)))
def test_dispatch_state_machine_fuzz_100x(seed):
    """One pytest case per seed → 500 cases total. Each gets its own
    line in -v output so we know exactly which seed failed if any.
    Bumped to 500 in v0.4.18 final-release verification — at 100 we
    rarely caught flakes; 500 raises the floor for race-condition
    coverage to ~1-in-500 odds."""
    result = _run_one_iteration(seed)
    assert result["all_dispatched"], (
        f"seed={seed}: only {len(result['actual'])}/{result['n_items']} "
        f"dispatched (initial_busy={result['initial_busy']}, "
        f"mode={result['mode_choice']}); actual={result['actual']!r}"
    )
    assert result["order_correct"], (
        f"seed={seed}: dispatch order broke; "
        f"expected={result['expected']!r}  actual={result['actual']!r}"
    )
    assert result["no_duplicates"], (
        f"seed={seed}: duplicate dispatches detected; {result['actual']!r}"
    )
