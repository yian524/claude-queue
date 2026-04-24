"""terminal_relay.py - bridge user's keyboard <-> claude's PTY.

Uses Windows ReadConsoleInputW via win_console_input to bypass IME / stdin
ANSI-response pollution problems that msvcrt.getwch has.

Threading model
---------------
  main thread:    reads key events (ConsoleInput), forwards to PTY or queue
  pty reader:     (owned by pty_host) writes PTY bytes to sys.stdout.buffer
  monitor thread: (owned by Monitor) dispatches queued messages

Modes
-----
  direct: every key goes straight to the PTY
  queue:  keys are buffered locally (echoed on stdout); Enter pushes buffer
          into queue_store and switches back to direct

Toggle
------
  Ctrl+Q toggles direct <-> queue
  Esc in queue mode cancels the buffered input
  Ctrl+C is always forwarded to the PTY
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import queue_store

# debug log: set CLAUDE_Q_DEBUG=1 to write every keystroke to a file
_DEBUG = os.environ.get("CLAUDE_Q_DEBUG", "") == "1"
_DEBUG_LOG = Path(os.path.expanduser("~/.claude/run/claude-q/relay_debug.log"))

try:
    import win_console_input as wci
    _HAS_WCI = True
except Exception:
    wci = None  # type: ignore[assignment]
    _HAS_WCI = False


# Virtual-key codes we care about
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_BACK = 0x08
VK_TAB = 0x09
VK_Q = 0x51
VK_C = 0x43


class TerminalRelay:
    """Main-thread key loop + mode state.

    Uses ReadConsoleInputW (win_console_input) so that:
      - IME doesn't steal Enter
      - Windows Terminal's ANSI replies to DA/CPR queries (which arrive via
        stdin as synthetic input) are ignored - they come through as
        character events with no keyboard backing, which we filter by
        requiring wVirtualKeyCode != 0 for typed chars or specific VKs for
        control keys.
    """

    def __init__(
        self,
        queue_path: Path,
        pty_write: Callable[[bytes], int],
        toggle_vk: int = VK_Q,
        on_mode_change: Optional[Callable[[str], None]] = None,
    ):
        self.queue_path = queue_path
        self.pty_write = pty_write
        self.toggle_vk = toggle_vk
        self.on_mode_change = on_mode_change

        self._mode = "direct"
        self._queue_buf: list[str] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------- public API -------------------------

    def get_mode(self) -> str:
        return self._mode

    def start(self) -> None:
        if not _HAS_WCI:
            raise RuntimeError("terminal_relay requires Windows ReadConsoleInputW "
                               "(win_console_input module)")
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------- input loop -------------------------

    def _loop(self) -> None:
        try:
            with wci.ConsoleInput() as ci:
                self._debug("ConsoleInput opened (ReadConsoleInputW mode)")
                while not self._stop.is_set():
                    k = ci.read_key(timeout_s=0.1)
                    if k is None:
                        continue
                    self._handle_key(k)
        except Exception as e:
            self._debug(f"relay loop crashed: {type(e).__name__}: {e}")
            sys.stdout.write(f"\r\n\x1b[31m[claude-q] relay loop crashed: {e}\x1b[0m\r\n")
            sys.stdout.flush()

    def _handle_key(self, k) -> None:
        """Process one decoded Key event."""
        # ---- Ctrl+Q: mode toggle ----
        if k.ctrl and k.vkey == self.toggle_vk:
            self._toggle_mode()
            return

        # ---- Ctrl+C: always forward; cancel queue buffer first ----
        if k.ctrl and k.vkey == VK_C:
            if self._mode == "queue":
                self._cancel_queue_input()
            self._send_to_pty(b"\x03")
            return

        # ---- Esc in queue mode: cancel ----
        if k.vkey == VK_ESCAPE and self._mode == "queue":
            self._cancel_queue_input()
            return

        # ---- Enter handling (VK_RETURN covers both IME-Enter and plain Enter) ----
        if k.vkey == VK_RETURN:
            if self._mode == "queue":
                self._commit_queue_input()
            else:
                payload = os.environ.get("CLAUDE_Q_ENTER", "cr").lower()
                if payload == "lf":
                    data = b"\n"
                elif payload == "crlf":
                    data = b"\r\n"
                else:
                    data = b"\r"
                self._debug(f"ENTER sent={data!r}")
                self._send_to_pty(data)
            return

        # ---- Arrow/function keys etc. (VT sequence precomputed) ----
        if k.vt is not None:
            if self._mode == "direct":
                self._debug(f"vt sent={k.vt!r} vk=0x{k.vkey:02X}")
                self._send_to_pty(k.vt.encode())
            # ignore in queue mode for MVP
            return

        # ---- Backspace ----
        if k.vkey == VK_BACK:
            if self._mode == "queue":
                if self._queue_buf:
                    self._queue_buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            else:
                self._debug("backspace sent")
                self._send_to_pty(b"\x7f")  # DEL - most Ink TUIs accept this
            return

        # ---- Tab ----
        if k.vkey == VK_TAB:
            if self._mode == "direct":
                self._send_to_pty(b"\t")
            return

        # ---- Plain character (includes Chinese via IME commit) ----
        if k.text:
            if self._mode == "queue":
                self._queue_buf.append(k.text)
                sys.stdout.write(k.text)
                sys.stdout.flush()
            else:
                self._debug(f"char sent={k.text!r} vk=0x{k.vkey:02X}")
                self._send_to_pty(k.text.encode("utf-8", errors="replace"))
            return

        # ---- Ctrl+letter / other ----
        if k.ctrl and 0x41 <= k.vkey <= 0x5A:
            # Ctrl+A..Z -> \x01..\x1A
            b = bytes([k.vkey - 0x40])
            self._debug(f"ctrl+{chr(k.vkey)} sent={b!r}")
            self._send_to_pty(b)
            return

        # fallback - log unknown
        self._debug(f"unhandled key vk=0x{k.vkey:02X} ctrl={k.ctrl} "
                    f"alt={k.alt} text={k.text!r}")

    # ------------------------- mode logic -------------------------

    def _toggle_mode(self) -> None:
        if self._mode == "direct":
            self._mode = "queue"
            self._queue_buf = []
            sys.stdout.write("\r\n\x1b[33m[claude-q] queue mode. type message + Enter "
                             "to queue, Esc to cancel, Ctrl+Q to toggle back.\x1b[0m\r\n"
                             "\x1b[35m[queue]>\x1b[0m ")
            sys.stdout.flush()
        else:
            self._cancel_queue_input(silent=True)
            self._mode = "direct"
            sys.stdout.write("\r\n\x1b[32m[claude-q] back to direct mode.\x1b[0m\r\n")
            sys.stdout.flush()
        if self.on_mode_change:
            try:
                self.on_mode_change(self._mode)
            except Exception:
                pass

    def _commit_queue_input(self) -> None:
        text = "".join(self._queue_buf).strip()
        self._queue_buf = []
        if not text:
            sys.stdout.write("\r\n\x1b[33m[claude-q] empty input; staying in queue mode.\x1b[0m\r\n"
                             "\x1b[35m[queue]>\x1b[0m ")
            sys.stdout.flush()
            return
        try:
            eid = queue_store.push(self.queue_path, text, source="queue-pane")
        except Exception as e:
            sys.stdout.write(f"\r\n\x1b[31m[claude-q] push failed: {e}\x1b[0m\r\n")
            sys.stdout.flush()
            return
        sys.stdout.write(f"\r\n\x1b[32m[claude-q] queued id={eid[:15]} "
                         f"preview={text[:60]!r}\x1b[0m\r\n")
        sys.stdout.write("\x1b[32m[claude-q] back to direct mode.\x1b[0m\r\n")
        sys.stdout.flush()
        self._mode = "direct"
        if self.on_mode_change:
            try:
                self.on_mode_change(self._mode)
            except Exception:
                pass

    def _cancel_queue_input(self, silent: bool = False) -> None:
        self._queue_buf = []
        if not silent:
            sys.stdout.write("\r\n\x1b[33m[claude-q] queue input cancelled.\x1b[0m\r\n")
            sys.stdout.flush()
        self._mode = "direct"
        if self.on_mode_change:
            try:
                self.on_mode_change(self._mode)
            except Exception:
                pass

    # ------------------------- PTY write wrapper -------------------------

    def _send_to_pty(self, data: bytes) -> None:
        try:
            self.pty_write(data)
        except Exception as e:
            sys.stdout.write(f"\r\n\x1b[31m[claude-q] pty write error: {e}\x1b[0m\r\n")
            sys.stdout.flush()

    def _debug(self, msg: str) -> None:
        if not _DEBUG:
            return
        try:
            _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _DEBUG_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass


# ------------------------------- self-test -------------------------------

def _self_test() -> int:
    """Non-interactive logic test (no keyboard needed)."""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        qpath = Path(td) / "queue.jsonl"
        written: list[bytes] = []

        r = TerminalRelay(
            queue_path=qpath,
            pty_write=lambda b: (written.append(b) or len(b)),
        )
        assert r.get_mode() == "direct"
        r._toggle_mode()
        assert r.get_mode() == "queue"
        for c in "hello":
            r._queue_buf.append(c)
        r._commit_queue_input()
        assert r.get_mode() == "direct"
        assert queue_store.pending_len(qpath) == 1
        head = queue_store.peek_pending(qpath)
        assert head is not None and head.text == "hello"

        r._toggle_mode()
        r._queue_buf.extend(list("abc"))
        r._cancel_queue_input(silent=True)
        assert r.get_mode() == "direct"
        assert queue_store.pending_len(qpath) == 1

    print("terminal_relay.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
