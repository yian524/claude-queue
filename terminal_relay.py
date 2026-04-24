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
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import queue_store

# ANSI alt-screen sequences
# - ?1049h: switch to alt screen + save cursor
# - ?25h:   show cursor
# - H + 2J: home + clear screen
# - ?1049l: exit alt screen + restore cursor
_ALT_ENTER = b"\x1b[?1049h\x1b[?25h\x1b[H\x1b[2J"
_ALT_EXIT = b"\x1b[?1049l"

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
        session_id: str = "",
    ):
        self.queue_path = queue_path
        self.pty_write = pty_write
        self.toggle_vk = toggle_vk
        self.on_mode_change = on_mode_change
        self.session_id = session_id

        self._mode = "direct"
        self._queue_buf: list[str] = []
        self._input_row = 0  # terminal row where input cursor lives (set by render)
        self._input_col_base = 0
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

        # ---- Enter family handling ----
        # - Plain Enter  -> submit (send \r)
        # - Ctrl+Enter   -> insert newline in input (send \n)
        # - Shift+Enter  -> same as Ctrl+Enter (common Ink TUI shortcut)
        # - Alt+Enter    -> same as Ctrl+Enter
        # In queue mode we only treat plain Enter as "commit"; modifier+Enter
        # inserts a literal newline into the queue buffer.
        if k.vkey == VK_RETURN:
            is_plain = not (k.ctrl or k.shift or k.alt)
            if self._mode == "queue":
                if is_plain:
                    self._commit_queue_input()
                else:
                    # newline inside queue buffer
                    self._queue_buf.append("\n")
                    self._update_input_line()
                return
            # direct mode
            if is_plain:
                payload = os.environ.get("CLAUDE_Q_ENTER", "cr").lower()
                if payload == "lf":
                    data = b"\n"
                elif payload == "crlf":
                    data = b"\r\n"
                else:
                    data = b"\r"
                self._debug(f"ENTER sent={data!r}")
                self._send_to_pty(data)
            else:
                # Ctrl/Shift/Alt + Enter -> literal newline
                self._debug(f"NEWLINE (modifier+Enter) sent=b'\\n' "
                            f"ctrl={k.ctrl} shift={k.shift} alt={k.alt}")
                self._send_to_pty(b"\n")
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
                    self._update_input_line()
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
                self._update_input_line()
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
    #
    # UX v2 (alt-screen based):
    #   * Entering queue mode switches the terminal to its alternate screen
    #     buffer (\x1b[?1049h), just like vim / less / tmux / htop do.
    #   * We draw a clean, full-screen queue UI there; Claude's main-screen
    #     TUI cannot redraw over us because the terminal shows only alt.
    #   * On exit (Enter / Esc / Ctrl+Q again), \x1b[?1049l tells the
    #     terminal to drop the alt screen and restore the main screen as it
    #     was — Claude's UI is perfectly preserved, no flush/redraw dance.

    def _toggle_mode(self) -> None:
        if self._mode == "direct":
            self._enter_queue_mode()
        else:
            # second Ctrl+Q acts like Esc-cancel
            self._exit_queue_mode(push=False)

    def _enter_queue_mode(self) -> None:
        self._mode = "queue"
        self._queue_buf = []
        # pause Claude->stdout passthrough BEFORE we switch screens so in-
        # flight ANSI doesn't land on our alt screen
        if self.on_mode_change:
            try:
                self.on_mode_change("queue")
            except Exception:
                pass
        try:
            sys.stdout.buffer.write(_ALT_ENTER)
            sys.stdout.buffer.flush()
        except Exception:
            pass
        self._render_queue_ui()

    def _exit_queue_mode(self, push: bool) -> None:
        text = "".join(self._queue_buf).strip() if push else ""
        self._queue_buf = []
        # Leave alt screen first — terminal restores Claude's UI as-was.
        try:
            sys.stdout.buffer.write(_ALT_EXIT)
            sys.stdout.buffer.flush()
        except Exception:
            pass
        self._mode = "direct"

        # Now print a short inline confirmation on the main screen.
        if push and text:
            try:
                eid = queue_store.push(self.queue_path, text, source="queue-pane")
                sys.stdout.write(
                    f"\r\n\x1b[32m[claude-q] queued id={eid[:15]} "
                    f"preview={text[:60]!r}\x1b[0m\r\n"
                )
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(f"\r\n\x1b[31m[claude-q] push failed: {e}\x1b[0m\r\n")
                sys.stdout.flush()
        # resume Claude->stdout passthrough AFTER alt screen exit so
        # buffered bytes reach the restored main screen cleanly.
        if self.on_mode_change:
            try:
                self.on_mode_change("direct")
            except Exception:
                pass

    def _commit_queue_input(self) -> None:
        text = "".join(self._queue_buf).strip()
        if not text:
            # empty input: stay in queue mode, redraw so user sees input area
            self._render_queue_ui(note="empty input; please type something or Esc to cancel")
            return
        self._exit_queue_mode(push=True)

    def _cancel_queue_input(self, silent: bool = False) -> None:
        self._exit_queue_mode(push=False)

    # ------------------------- UI rendering -------------------------

    def _render_queue_ui(self, note: str = "") -> None:
        """Draw the full queue UI on the alt screen.

        Tracks the row of the input line as it builds, and at the end emits
        an explicit cursor-position sequence so the terminal cursor ends up
        at the end of the user's input buffer (inside the box) — not stranded
        at the bottom-left of the screen below the box.
        """
        try:
            cols = max(60, shutil.get_terminal_size((80, 24)).columns)
        except Exception:
            cols = 80

        pending = [
            e for e in queue_store.list_all(self.queue_path)
            if e.status == queue_store.STATUS_PENDING
        ]

        sid = (self.session_id or "(no-session)")[:30]
        out: list[str] = []
        row = 0

        def add(line: str) -> None:
            nonlocal row
            out.append(line + "\r\n")
            row += 1

        # Hide cursor during draw to avoid flicker
        out.append("\x1b[?25l")
        out.append("\x1b[H\x1b[2J")  # home + clear

        # top banner (yellow)
        add("\x1b[1;33m╔" + "═" * (cols - 2) + "╗\x1b[0m")
        title = "  [claude-q]  QUEUE INPUT"
        right = f"session: {sid}  "
        gap = cols - 2 - len(title) - len(right)
        add("\x1b[1;33m║\x1b[0m" + title + " " * max(0, gap) + right
            + "\x1b[1;33m║\x1b[0m")
        add("\x1b[1;33m╠" + "═" * (cols - 2) + "╣\x1b[0m")

        # pending list (cyan)
        header = f"  Pending ({len(pending)}):"
        add("\x1b[36m║\x1b[0m" + header + " " * max(0, cols - 2 - len(header))
            + "\x1b[36m║\x1b[0m")
        shown = pending[-8:]
        if shown:
            start_idx = max(1, len(pending) - 7)
            for i, e in enumerate(shown, start=start_idx):
                preview = e.text.replace("\n", " ")
                if len(preview) > cols - 12:
                    preview = preview[:cols - 15] + "..."
                line = f"    {i:>2}. {preview}"
                pad = cols - 2 - len(line)
                add("\x1b[36m║\x1b[0m" + line + " " * max(0, pad)
                    + "\x1b[36m║\x1b[0m")
        else:
            line = "    (empty)"
            pad = cols - 2 - len(line)
            add("\x1b[36m║\x1b[0m\x1b[2m" + line + " " * max(0, pad)
                + "\x1b[0m\x1b[36m║\x1b[0m")

        add("\x1b[36m╠" + "═" * (cols - 2) + "╣\x1b[0m")

        instr = "  Enter=queue  Esc / Ctrl+Q=cancel  Backspace=delete"
        add("\x1b[36m║\x1b[0m" + instr + " " * max(0, cols - 2 - len(instr))
            + "\x1b[36m║\x1b[0m")
        if note:
            hint = "  " + note
            add("\x1b[36m║\x1b[0m\x1b[33m" + hint + "\x1b[0m"
                + " " * max(0, cols - 2 - len(hint)) + "\x1b[36m║\x1b[0m")

        # empty spacer row ABOVE the input line
        add("\x1b[36m║" + " " * (cols - 2) + "║\x1b[0m")

        # === INPUT LINE — remember its row so we can park cursor here ===
        input_row = row + 1  # ANSI rows are 1-based
        buf = "".join(self._queue_buf)
        prompt_visible = "  > "          # 4 cols: "  ", ">", " "
        # keep the right side of buf visible if it overflows
        max_buf_visible = cols - 2 - len(prompt_visible) - 1
        buf_display = buf if len(buf) <= max_buf_visible else buf[-max_buf_visible:]
        line_text = (
            "\x1b[36m║\x1b[0m"                      # left border (no width)
            + "  "                                   # 2-col indent
            + "\x1b[1;35m>\x1b[0m "                  # coloured prompt + space
            + buf_display
        )
        visible_len = 2 + 2 + len(buf_display)       # "  " + "> " + buf
        pad = cols - 2 - visible_len
        add(line_text + " " * max(0, pad) + "\x1b[36m║\x1b[0m")

        # cursor column: after "║" (col 1) + "  " (2 cols) + "> " (2 cols) + buf
        cursor_col = 1 + 2 + 2 + len(buf_display) + 1  # +1 because 1-based

        add("\x1b[36m║" + " " * (cols - 2) + "║\x1b[0m")
        add("\x1b[36m╚" + "═" * (cols - 2) + "╝\x1b[0m")

        # park cursor at input line, then show it again
        out.append(f"\x1b[{input_row};{cursor_col}H")
        out.append("\x1b[?25h")

        payload = "".join(out).encode("utf-8", errors="replace")
        try:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def _update_input_line(self) -> None:
        """Fast-path redraw: just re-render the whole UI (no cursor math).

        Called on every char / backspace in queue mode. Full re-render is
        cheap in alt screen because the terminal has only this small UI to
        draw — no interference with Claude's main screen.
        """
        self._render_queue_ui()

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
