"""monitor.py — background dispatcher thread.

Loop:
  1. capture PTY tail via pty_host.tail()
  2. run idle_detector.is_idle()
  3. if idle AND queue has pending AND we weren't dispatching 1s ago:
       entry = queue_store.pop_pending()
       briefly re-verify still idle (dispatch_commit_delay_s)
       pty_host.write(entry.text + "\r")
       back off post_dispatch_backoff_s so next capture sees "busy"

Safety rails
------------
* Monitor never dispatches during the first 2 seconds after spawn
  (let claude finish its initial TUI render).
* If a dispatch fails (pty write raises), the entry is re-marked as pending
  via push_front-like fallback (we append a new pending entry with the
  same text; the old one stays marked sent with an error note in monitor.log).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import idle_detector
import queue_store
from idle_detector import IdleState


@dataclass
class MonitorState:
    """Shared mutable state read by status_bar / terminal_relay."""
    idle: bool = False
    queue_len: int = 0          # total pending (including scheduled-future)
    ready_len: int = 0          # pending AND dispatchable now (schedule matured)
    last_dispatch_at: float = 0.0
    # Has Claude actually entered the "busy" state since we last dispatched?
    # Used to prevent back-to-back dispatches when Claude hasn't caught up
    # to the previous one yet (race: idle detector briefly says True
    # because Claude's busy marker hasn't surfaced in the tail buffer yet).
    saw_busy_since_dispatch: bool = True  # True on startup (no prior dispatch)
    drift_detected: bool = False
    last_reasons: dict = None  # type: ignore[assignment]
    dispatched_total: int = 0
    error: Optional[str] = None


class Monitor:
    def __init__(
        self,
        run_dir: Path,
        pty_tail_fn: Callable[[], str],    # () -> str (pty tail)
        pty_write_fn: Callable[[bytes], int],  # (bytes) -> int
        get_mode: Callable[[], str],       # () -> "direct"|"queue"
        poll_interval_s: float = 0.1,
        debounce_s: float = 0.25,
        dispatch_commit_delay_s: float = 0.03,
        post_dispatch_backoff_s: float = 0.5,
        prompt_no_match_warn_s: float = 30.0,
        startup_grace_s: float = 0.5,
        force_dispatch_after_stuck_s: float = 5.0,
    ):
        self.run_dir = run_dir
        self.queue_path = run_dir / "queue.jsonl"
        self.log_path = run_dir / "monitor.log"

        self.pty_tail_fn = pty_tail_fn
        self.pty_write_fn = pty_write_fn
        self.get_mode = get_mode

        self.poll_interval_s = poll_interval_s
        self.debounce_s = debounce_s
        self.dispatch_commit_delay_s = dispatch_commit_delay_s
        self.post_dispatch_backoff_s = post_dispatch_backoff_s
        self.prompt_no_match_warn_s = prompt_no_match_warn_s
        self.startup_grace_s = startup_grace_s
        # Safety net: when prompt detection fails (e.g. Claude TUI shipped a
        # new screen format that breaks PROMPT_RE), allow dispatch anyway
        # IF the queue has been stuck for this long AND the screen content
        # is stable AND no busy markers are visible. The "stable for N
        # seconds" check is the load-bearing guarantee — even if our busy
        # regex is missing a new format, real generation produces constant
        # screen churn, so a stable window is a reliable proxy for
        # "Claude is actually idle". Set to 0 to disable.
        #
        # Default 5s (v0.4.11): once L1 prompt-scan window widened to
        # 30 lines AND we tracked _continuously_stable_since (vs the
        # old "stable AT this exact tick" check), the worst-case
        # fallback shrunk from 15s to 5s without false positives. Real
        # Claude generation produces nonstop screen churn, so 5
        # continuous seconds of stable+not-busy is a strong idle
        # signal even if PROMPT_RE failed entirely.
        self.force_dispatch_after_stuck_s = force_dispatch_after_stuck_s

        self.state = MonitorState(last_reasons={})
        self._idle_state = IdleState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logger = self._make_logger()
        self._started_at = 0.0
        # Tick-loop transient state. Declared here (rather than relying
        # on getattr-with-default) so type-checkers and grep can see
        # the lifetime; reviewer (3560bd1 review) flagged the implicit
        # creation as a maintainability concern.
        self._dispatch_stuck_since: float = 0.0
        self._continuously_stable_since: float = 0.0
        self._last_block_log: float = 0.0
        self._last_tail_dump: float = 0.0

    # ------------------------- lifecycle -------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()
        self._logger.info("monitor started")

    def stop(self) -> None:
        self._stop.set()
        self._logger.info("monitor stopping")

    def snapshot(self) -> dict:
        # seconds we've been holding dispatch; 0 if not stuck
        stuck_since = getattr(self, "_dispatch_stuck_since", 0)
        stuck_s = (time.monotonic() - stuck_since) if stuck_since else 0
        return {
            "idle": self.state.idle,
            "queue_len": self.state.queue_len,
            "ready_len": self.state.ready_len,
            "drift_detected": self.state.drift_detected,
            "last_reasons": self.state.last_reasons,
            "dispatched_total": self.state.dispatched_total,
            "last_dispatch_at": self.state.last_dispatch_at,
            "stuck_seconds": round(stuck_s, 1),
            "mode": self.get_mode(),
            "error": self.state.error,
        }

    # ------------------------- internals -------------------------

    def _make_logger(self) -> logging.Logger:
        lg = logging.getLogger(f"claude-queue.monitor.{self.run_dir.name}")
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            h = logging.FileHandler(self.log_path, encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            ))
            lg.addHandler(h)
            lg.propagate = False
        return lg

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self._tick()
            except Exception as e:
                self.state.error = f"{type(e).__name__}: {e}"
                self._logger.exception("monitor tick failed")

    def _tick(self) -> None:
        now = time.monotonic()
        prev_ready = self.state.ready_len
        # update queue length (total pending; both scheduled-future and ready)
        self.state.queue_len = queue_store.pending_len(self.queue_path)
        # track dispatch-ready (eligible NOW) — what monitor actually may send
        self.state.ready_len = queue_store.dispatch_ready_len(self.queue_path)
        # Log every time the ready-set changes (an entry matured or was
        # dispatched); helps diagnose "why didn't my /wait fire?" reports.
        if self.state.ready_len != prev_ready:
            self._logger.info(
                f"ready_len changed {prev_ready} -> {self.state.ready_len} "
                f"(total pending={self.state.queue_len})"
            )

        tail = self.pty_tail_fn()
        result = idle_detector.is_idle(
            tail_output=tail,
            state=self._idle_state,
            now=now,
            debounce_s=self.debounce_s,
            prompt_no_match_warn_s=self.prompt_no_match_warn_s,
        )
        idle_detector.apply_result(self._idle_state, result)

        self.state.idle = result.idle
        self.state.drift_detected = result.drift_detected
        self.state.last_reasons = result.reasons

        # The moment Claude goes non-idle AFTER we dispatched, we know
        # our message landed and Claude is processing it. Remember this
        # so the next dispatch can proceed confidently.
        if not result.idle and not self.state.saw_busy_since_dispatch:
            self.state.saw_busy_since_dispatch = True
            self._logger.info("confirmed Claude went busy after dispatch; "
                              "cleared saw_busy_since_dispatch latch")

        # don't dispatch during startup grace, during queue mode, or within
        # post_dispatch_backoff of the last send
        # Track how long we've been stuck holding dispatch so we can
        # surface a user-visible warning (title bar) if Claude is in an
        # unusual state (e.g. "Esc again to clear") and the queue is
        # frozen. See _dispatch_stuck_since.
        if self.state.ready_len > 0 and not result.idle:
            if getattr(self, "_dispatch_stuck_since", 0) == 0:
                self._dispatch_stuck_since = now
        else:
            self._dispatch_stuck_since = 0

        # Log blocking reasons ONLY when we have ready entries; this is
        # the signal for "why didn't my /wait fire?".
        if self.state.ready_len > 0 and not result.idle:
            # throttle: log at most every 3 seconds
            if now - getattr(self, "_last_block_log", 0) > 3.0:
                self._logger.info(
                    f"holding dispatch: ready={self.state.ready_len} "
                    f"reasons={result.reasons} drift={result.drift_detected}"
                )
                self._last_block_log = now
                # If we've been blocked for over 10s, dump the actual tail
                # so we can diagnose what the detector is seeing. Dumped
                # once per 30-second window to avoid log bloat.
                if result.drift_detected:
                    last_dump = getattr(self, "_last_tail_dump", 0)
                    if now - last_dump > 30.0:
                        import idle_detector as _ide
                        clean = _ide._strip_ansi(tail)
                        lines = [l for l in clean.splitlines() if l.strip()]
                        last5 = lines[-5:]
                        self._logger.info(
                            "tail dump (last 5 non-empty stripped lines):"
                        )
                        for ln in last5:
                            # truncate very long lines
                            disp = ln if len(ln) <= 120 else ln[:117] + "..."
                            self._logger.info(f"  | {disp!r}")
                        self._last_tail_dump = now

        if now - self._started_at < self.startup_grace_s:
            return
        # NOTE: previously gated on `get_mode() == "direct"` here, which
        # meant queued items would NOT dispatch while the user sat in
        # queue mode (Ctrl+Q UI). That contradicted the whole "fire and
        # forget" point of a queue: enqueueing an item is an explicit
        # signal the user wants it sent, so we should dispatch as soon
        # as Claude is idle, no matter which mode the user is composing
        # in. The queue input pane is a separate UI buffer that the
        # dispatcher does not touch — Claude's response renders in the
        # main pane above the queue overlay, fully visible.
        if now - self.state.last_dispatch_at < self.post_dispatch_backoff_s:
            return

        # ---- L2 safety override ----
        # If the canonical idle check fails BUT the screen has been
        # CONTINUOUSLY stable AND not_busy for `force_dispatch_after_stuck_s`
        # seconds, dispatch anyway. The "continuously stable" check is much
        # more reliable than the previous "stuck for N AND stable at this
        # exact tick" — the old logic required all three signals to align
        # at the same poll tick, which a churning status bar would foil
        # repeatedly, adding 5+ seconds of jitter on top of the threshold.
        #
        # Real Claude generation produces nonstop screen churn (token
        # stream + spinner animation), so any continuous-stable window is
        # an extremely strong proxy for genuine idle, even if PROMPT_RE
        # can't find the prompt glyph.
        if (result.reasons.get("not_busy") is True
                and result.reasons.get("stable") is True):
            if getattr(self, "_continuously_stable_since", 0) == 0:
                self._continuously_stable_since = now
        else:
            self._continuously_stable_since = 0

        stable_for = (
            (now - self._continuously_stable_since)
            if getattr(self, "_continuously_stable_since", 0) else 0
        )

        forced = False
        if (not result.idle
                and self.force_dispatch_after_stuck_s > 0
                and self.state.ready_len > 0
                and stable_for >= self.force_dispatch_after_stuck_s):
            self._logger.warning(
                f"FORCED dispatch after {stable_for:.1f}s continuously "
                f"stable+idle (prompt regex outdated; "
                f"reasons={result.reasons})"
            )
            forced = True

        if not result.idle and not forced:
            return
        # Critical race guard: after we dispatch, require that Claude was
        # OBSERVED busy at least once before we allow another dispatch.
        # Otherwise the idle detector can briefly say "idle=True" for a
        # moment right after our write because Claude's busy indicator
        # hasn't propagated through the PTY tail yet, causing us to
        # dispatch a second queued entry immediately — which Claude
        # then concatenates with the first.
        if not self.state.saw_busy_since_dispatch:
            # safety fallback: if too much time has passed and Claude still
            # looks idle (i.e. our previous write probably never reached
            # Claude — e.g. dispatch payload was empty), release the latch
            # so we don't stall the queue forever.
            stale_s = now - self.state.last_dispatch_at
            if stale_s < 15.0:
                return
            self._logger.info(
                f"stale latch cleared after {stale_s:.0f}s idle post-dispatch"
            )
            self.state.saw_busy_since_dispatch = True
        # use ready_len (not queue_len) so scheduled-future entries don't
        # trigger dispatch — they wait in the queue until their dispatch_at
        # matures, then become "ready".
        if self.state.ready_len <= 0:
            return

        # commit window: re-check after a short delay that idle still holds
        time.sleep(self.dispatch_commit_delay_s)
        tail2 = self.pty_tail_fn()
        now2 = time.monotonic()
        r2 = idle_detector.is_idle(
            tail_output=tail2,
            state=self._idle_state,
            now=now2,
            debounce_s=self.debounce_s,
            prompt_no_match_warn_s=self.prompt_no_match_warn_s,
        )
        idle_detector.apply_result(self._idle_state, r2)
        # For a forced dispatch we don't insist on r2.idle (the prompt regex
        # is broken by definition in that path); we only re-confirm that
        # NO busy marker has appeared in the meantime — i.e. nothing has
        # started generating during the commit-window sleep.
        if forced:
            if r2.reasons.get("not_busy") is False:
                self._logger.info(
                    "forced dispatch aborted: busy marker appeared during "
                    "commit window (regex caught up)"
                )
                return
        elif not r2.idle:
            self._logger.info("dispatch aborted: idle flipped during commit window")
            return

        entry = queue_store.pop_pending(self.queue_path)
        if entry is None:
            return

        # actually send: text + carriage return (claude uses CR as submit)
        try:
            payload = (entry.text.rstrip("\r\n") + "\r").encode("utf-8")
            self.pty_write_fn(payload)
            self.state.last_dispatch_at = now2
            self.state.dispatched_total += 1
            # Arm the "must see busy" latch: next dispatch is held until
            # Claude actually enters busy state after this one.
            self.state.saw_busy_since_dispatch = False
            self._logger.info(f"dispatched id={entry.id} text_preview={entry.text[:60]!r}")
        except Exception as e:
            self._logger.error(f"dispatch write failed for id={entry.id}: {e}")
            # re-queue at tail (simplest recovery; keeps ordering roughly)
            try:
                queue_store.push(self.queue_path, entry.text, source="requeue-after-error")
            except Exception:
                pass
            self.state.error = f"dispatch write failed: {e}"


# ------------------------------- self-test -------------------------------

def _self_test() -> int:
    import tempfile as _tf

    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        run_dir = Path(td)
        written: list[bytes] = []

        fake_tail = ["<banner>"]

        def tail_fn() -> str:
            return fake_tail[0]

        def write_fn(b: bytes) -> int:
            written.append(b)
            # after first dispatch simulate claude becoming busy
            fake_tail[0] = "processing..."
            return len(b)

        mode = ["direct"]

        mon = Monitor(
            run_dir=run_dir,
            pty_tail_fn=tail_fn,
            pty_write_fn=write_fn,
            get_mode=lambda: mode[0],
            poll_interval_s=0.05,
            debounce_s=0.1,
            dispatch_commit_delay_s=0.02,
            post_dispatch_backoff_s=0.2,
            startup_grace_s=0.1,
        )

        # push a message before start
        queue_store.push(run_dir / "queue.jsonl", "hello world")

        # simulate idle pane
        fake_tail[0] = (
            "some previous claude output\n"
            "╭──────────────────────────╮\n"
            "│ >                        │\n"
            "╰──────────────────────────╯\n"
        )

        mon.start()
        # wait up to 3s for dispatch
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if written:
                break
            time.sleep(0.05)
        mon.stop()
        # let loop wind down
        time.sleep(0.2)

        assert len(written) == 1, f"expected 1 dispatched write, got {len(written)}"
        assert b"hello world" in written[0]
        assert queue_store.pending_len(run_dir / "queue.jsonl") == 0

        # release file handles so tempdir cleanup works on Windows
        for h in list(mon._logger.handlers):
            h.close()
            mon._logger.removeHandler(h)

    # ------------------------------------------------------------------
    # L2 regression: force-dispatch when the prompt-glyph regex fails.
    # Simulates Claude Code 2.1.x rendering the input pane in a layout
    # PROMPT_RE_END/PROMPT_RE_LINE can't see, with no busy markers and a
    # stable screen — the queue must still drain, not stall forever.
    # ------------------------------------------------------------------
    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        run_dir = Path(td)
        written: list[bytes] = []

        # Tail that NO PROMPT_RE pattern matches (no ❯ ›  〉 │>│ anywhere)
        # AND contains no busy markers — the exact failure mode 2.1.121
        # produced. Stable across calls (same string), so the `stable`
        # signal flips True quickly.
        unrecognized_tail = (
            "previous answer text on screen\n"
            "no recognizable prompt glyph here\n"
            "definitely no spinner or ellipsis\n"
        )
        fake_tail = [unrecognized_tail]

        def tail_fn() -> str:
            return fake_tail[0]

        def write_fn(b: bytes) -> int:
            written.append(b)
            return len(b)

        mode = ["direct"]
        mon = Monitor(
            run_dir=run_dir,
            pty_tail_fn=tail_fn,
            pty_write_fn=write_fn,
            get_mode=lambda: mode[0],
            poll_interval_s=0.02,
            debounce_s=0.05,
            dispatch_commit_delay_s=0.01,
            post_dispatch_backoff_s=0.05,
            startup_grace_s=0.05,
            # Aggressive threshold for the test: 1 second instead of 60s.
            force_dispatch_after_stuck_s=1.0,
        )
        queue_store.push(run_dir / "queue.jsonl", "force-dispatched payload")

        mon.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if written:
                break
            time.sleep(0.05)
        mon.stop()
        time.sleep(0.2)

        assert len(written) == 1, (
            f"L2 regression: queue should have force-dispatched after "
            f"~1s of being stuck on an unrecognized prompt; got "
            f"{len(written)} writes"
        )
        assert b"force-dispatched payload" in written[0]

        for h in list(mon._logger.handlers):
            h.close()
            mon._logger.removeHandler(h)

    # ------------------------------------------------------------------
    # L2 negative case: when the screen is NOT stable (genuine generation
    # in progress) the force-dispatch must NOT fire, even after the stuck
    # timer crosses the threshold.
    # ------------------------------------------------------------------
    with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        run_dir = Path(td)
        written: list[bytes] = []

        counter = [0]

        def tail_fn_churning() -> str:
            counter[0] += 1
            # Different content on every call → never stable → never
            # eligible for force dispatch.
            return f"churning output {counter[0]}\nno prompt visible\n"

        def write_fn(b: bytes) -> int:
            written.append(b)
            return len(b)

        mode = ["direct"]
        mon = Monitor(
            run_dir=run_dir,
            pty_tail_fn=tail_fn_churning,
            pty_write_fn=write_fn,
            get_mode=lambda: mode[0],
            poll_interval_s=0.02,
            debounce_s=0.05,
            dispatch_commit_delay_s=0.01,
            post_dispatch_backoff_s=0.05,
            startup_grace_s=0.05,
            force_dispatch_after_stuck_s=0.5,
        )
        queue_store.push(run_dir / "queue.jsonl", "must NOT dispatch")

        mon.start()
        time.sleep(2.0)  # well past the 0.5s force threshold
        mon.stop()
        time.sleep(0.2)

        assert len(written) == 0, (
            f"L2 safety: must not force-dispatch into a churning screen "
            f"(would interrupt active generation); got {len(written)} writes"
        )

        for h in list(mon._logger.handlers):
            h.close()
            mon._logger.removeHandler(h)

    print("monitor.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
