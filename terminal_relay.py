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
import unicodedata
from pathlib import Path
from typing import Callable, Optional

import queue_store
import scheduler as _scheduler
import slash_commands as _slash


def _visual_width(text: str) -> int:
    """Return the on-screen column width of a string.

    CJK / emoji / fullwidth characters take 2 columns; everything else
    takes 1. This matters for cursor positioning inside the queue UI:
    using `len()` undercounts when the buffer contains Chinese, so the
    ANSI cursor lands too far left and appears outside the input box.
    """
    w = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        elif unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            w += 0  # combining / zero-width
        else:
            w += 1
    return w

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
VK_UP = 0x26
VK_DOWN = 0x28
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_HOME = 0x24
VK_END = 0x23
VK_DELETE = 0x2E


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
        # Cursor position WITHIN _queue_buf, measured in list indices
        # (not visual columns). Range: 0..len(_queue_buf).
        # len() == at end of buffer; 0 == at very start.
        self._cursor_pos: int = 0
        self._input_row = 0  # terminal row where input cursor lives (set by render)
        self._input_col_base = 0
        # --- dropdown autocomplete state (queue mode) ---
        self._dropdown_active: bool = False
        self._dropdown_items: list[dict] = []
        self._dropdown_selected: int = 0
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
            sys.stdout.write(f"\r\n\x1b[31m[claude -q] relay loop crashed: {e}\x1b[0m\r\n")
            sys.stdout.flush()

    def _handle_key(self, k) -> None:
        """Process one decoded Key event."""
        # ---- Dropdown navigation (queue mode, dropdown open) ----
        # Arrow keys + Tab + Enter are intercepted BEFORE normal handling so
        # the dropdown takes priority.
        if self._mode == "queue" and self._dropdown_active:
            if k.vkey == VK_UP:
                if self._dropdown_items:
                    self._dropdown_selected = (
                        (self._dropdown_selected - 1) % len(self._dropdown_items)
                    )
                    self._update_input_line()
                return
            if k.vkey == VK_DOWN:
                if self._dropdown_items:
                    self._dropdown_selected = (
                        (self._dropdown_selected + 1) % len(self._dropdown_items)
                    )
                    self._update_input_line()
                return
            if k.vkey == VK_TAB:
                self._apply_dropdown_selection()
                return
            # Enter with dropdown open: also apply selection (confirms template
            # rather than pushing a raw '/wait' message)
            if k.vkey == VK_RETURN:
                self._apply_dropdown_selection()
                return
            # Esc while dropdown open: close dropdown, stay in queue mode
            if k.vkey == VK_ESCAPE:
                self._dropdown_active = False
                self._dropdown_items = []
                self._update_input_line()
                return

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

        # ---- Queue-mode cursor navigation & edit keys ----
        # MUST come before the generic `k.vt is not None` block below,
        # because Left/Right/Home/End/Delete all have `k.vt` set (they're
        # translated to VT sequences for direct-mode passthrough), and
        # that block returns early in queue mode.
        if self._mode == "queue":
            if k.vkey == VK_LEFT:
                if self._cursor_pos > 0:
                    self._cursor_pos -= 1
                    self._update_input_line()
                return
            if k.vkey == VK_RIGHT:
                if self._cursor_pos < len(self._queue_buf):
                    self._cursor_pos += 1
                    self._update_input_line()
                return
            if k.vkey == VK_HOME:
                if self._cursor_pos != 0:
                    self._cursor_pos = 0
                    self._update_input_line()
                return
            if k.vkey == VK_END:
                if self._cursor_pos != len(self._queue_buf):
                    self._cursor_pos = len(self._queue_buf)
                    self._update_input_line()
                return
            if k.vkey == VK_DELETE:
                if self._cursor_pos < len(self._queue_buf):
                    del self._queue_buf[self._cursor_pos]
                    self._refresh_dropdown()
                    self._update_input_line()
                return
            if k.vkey == VK_BACK:
                if self._cursor_pos > 0:
                    del self._queue_buf[self._cursor_pos - 1]
                    self._cursor_pos -= 1
                    self._refresh_dropdown()
                    self._update_input_line()
                return

        # ---- Arrow/function keys etc. (direct-mode passthrough) ----
        if k.vt is not None:
            if self._mode == "direct":
                self._debug(f"vt sent={k.vt!r} vk=0x{k.vkey:02X}")
                self._send_to_pty(k.vt.encode())
            return

        # ---- Backspace / Delete in DIRECT mode ----
        if k.vkey == VK_BACK:
            self._debug("backspace sent")
            self._send_to_pty(b"\x7f")
            return
        if k.vkey == VK_DELETE:
            self._send_to_pty(b"\x1b[3~")
            return

        # ---- Tab ----
        if k.vkey == VK_TAB:
            if self._mode == "direct":
                self._send_to_pty(b"\t")
            return

        # ---- Plain character (includes Chinese via IME commit) ----
        if k.text:
            if self._mode == "queue":
                # Insert AT cursor position (not always at end)
                self._queue_buf.insert(self._cursor_pos, k.text)
                self._cursor_pos += 1
                self._refresh_dropdown()
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
        self._cursor_pos = 0
        self._dropdown_active = False
        self._dropdown_items = []
        self._dropdown_selected = 0
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

    def _exit_queue_mode(self, push: bool, parsed=None) -> None:
        """Exit queue mode and act on parsed command (if any).

        If `parsed` is provided and is a QueueRequest, it carries the
        text + dispatch_at + priority to push. If ForceSendRequest,
        write directly to PTY bypassing queue. Otherwise (push=True
        without parsed) falls back to plain-text push.
        """
        raw_text = "".join(self._queue_buf).strip() if push else ""
        self._queue_buf = []
        self._cursor_pos = 0
        # Leave alt screen first — terminal restores Claude's UI as-was.
        try:
            sys.stdout.buffer.write(_ALT_EXIT)
            sys.stdout.buffer.flush()
        except Exception:
            pass
        self._mode = "direct"

        # Act on the parsed command (or fall back to plain push).
        # Confirmation messages go to the WINDOW TITLE (OSC 0) not the
        # main screen, so they don't fight with Claude's Ink TUI for
        # screen real estate. The cursor stays on Claude's own input
        # prompt — which is what the user wants.
        if push:
            try:
                if isinstance(parsed, _slash.ForceSendRequest):
                    self._send_to_pty((parsed.text.rstrip("\r\n") + "\r")
                                      .encode("utf-8"))
                    self._set_title(f"claude -q /now sent: "
                                    f"{parsed.text[:40]!r}")
                elif isinstance(parsed, _slash.QueueRequest):
                    eid = queue_store.push(
                        self.queue_path,
                        parsed.text,
                        source="queue-pane",
                        dispatch_at=parsed.dispatch_at,
                        priority=parsed.priority,
                    )
                    when = (_scheduler.humanize_delta(parsed.dispatch_at)
                            if parsed.dispatch_at else "ASAP")
                    tag = "priority" if parsed.priority > 0 else "queued"
                    n = queue_store.pending_len(self.queue_path)
                    self._set_title(
                        f"claude -q {tag} ({when}) pending:{n} - "
                        f"{parsed.text[:40]!r}"
                    )
                elif raw_text:
                    eid = queue_store.push(self.queue_path, raw_text,
                                           source="queue-pane")
                    n = queue_store.pending_len(self.queue_path)
                    self._set_title(
                        f"claude -q queued pending:{n} - {raw_text[:40]!r}"
                    )
            except Exception as e:
                # Errors DO go to main screen because user must see them
                sys.stdout.write(
                    f"\r\n\x1b[31m[claude -q] push failed: {e}\x1b[0m\r\n"
                )
                sys.stdout.flush()

        # resume Claude->stdout passthrough AFTER alt screen exit so
        # buffered bytes reach the restored main screen cleanly.
        if self.on_mode_change:
            try:
                self.on_mode_change("direct")
            except Exception:
                pass

    def _commit_queue_input(self) -> None:
        raw = "".join(self._queue_buf).strip()
        if not raw:
            self._queue_buf = []
            self._cursor_pos = 0
            self._render_queue_ui(
                note="empty input; please type something or Esc to cancel"
            )
            return

        # parse for /slash commands
        parsed = _slash.parse(raw)

        if isinstance(parsed, _slash.ParseError):
            self._render_queue_ui(note=parsed.message)
            return
        if isinstance(parsed, _slash.HelpRequest):
            self._render_queue_ui(note=self._help_text())
            return
        if isinstance(parsed, _slash.DropRequest):
            self._handle_drop(parsed.index)
            return
        if isinstance(parsed, _slash.ClearRequest):
            self._handle_clear()
            return
        # (CancelRequest removed in v0.3.1 — Esc / Ctrl+Q already cancel)

        # QueueRequest or ForceSendRequest: exit mode and push/send
        self._exit_queue_mode(push=True, parsed=parsed)

    @staticmethod
    def _help_text() -> str:
        """One-line help shown in queue UI when user types /help."""
        return ("/wait <dur> msg | /at <time> msg | /priority msg | /now msg "
                "| /drop N | /clear | /help  (Esc or Ctrl+Q cancels)")

    # ------------------------- /drop /clear handlers -------------------------

    def _handle_drop(self, index: int) -> None:
        """Drop Pending entry at 1-based index N, then stay in queue mode."""
        self._queue_buf = []
        pending = [e for e in queue_store.list_all(self.queue_path)
                   if e.status == queue_store.STATUS_PENDING]
        if index < 1 or index > len(pending):
            self._render_queue_ui(
                note=f"/drop: index {index} out of range (have {len(pending)})"
            )
            return
        # Show by dispatch order (same as render order)
        pending_sorted = sorted(pending, key=queue_store._rank)
        target = pending_sorted[index - 1]
        ok = queue_store.drop(self.queue_path, target.id)
        if ok:
            self._render_queue_ui(
                note=f"dropped: {target.text[:50]!r}"
            )
        else:
            self._render_queue_ui(note="/drop: already dropped?")

    def _handle_clear(self) -> None:
        self._queue_buf = []
        n = queue_store.clear(self.queue_path)
        self._render_queue_ui(note=f"/clear: dropped {n} entries")

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
        # Show in the same order the monitor will dispatch them
        # (priority desc, dispatch_at asc, ts asc).
        pending.sort(key=queue_store._rank)

        # Read the monitor's most recent snapshot so we can explain to the
        # user WHY a pending-ASAP entry isn't dispatching (common source of
        # confusion: Claude has draft text in its input -> prompt_visible=
        # False -> monitor holds dispatch).
        dispatch_hint = self._dispatch_hint(pending)

        sid = (self.session_id or "(no-session)")[:30]
        out: list[str] = []
        row = 0

        def add(line: str) -> None:
            """Append a rendered line. Each line is prefixed with \\x1b[K to
            clear anything that might have been on that row (defensive — in
            case a prior frame left artefacts that the screen-clear missed).
            """
            nonlocal row
            out.append("\x1b[K" + line + "\r\n")
            row += 1

        # Hide cursor during draw to avoid flicker + any stale glyph
        out.append("\x1b[?25l")
        # Home + erase-to-end-of-screen (safer than \x1b[2J on some terms)
        out.append("\x1b[H\x1b[J")

        # top banner (yellow)
        add("\x1b[1;33m╔" + "═" * (cols - 2) + "╗\x1b[0m")
        title = "  [claude -q]  QUEUE INPUT"
        right = f"session: {sid}  "
        gap = cols - 2 - len(title) - len(right)
        add("\x1b[1;33m║\x1b[0m" + title + " " * max(0, gap) + right
            + "\x1b[1;33m║\x1b[0m")
        add("\x1b[1;33m╠" + "═" * (cols - 2) + "╣\x1b[0m")

        # pending list (cyan)
        header = f"  Pending ({len(pending)}):"
        add("\x1b[36m║\x1b[0m" + header + " " * max(0, cols - 2 - len(header))
            + "\x1b[36m║\x1b[0m")
        shown = pending[:8]   # first 8 in dispatch order
        if shown:
            for i, e in enumerate(shown, start=1):
                preview = e.text.replace("\n", " ")
                # build the scheduling suffix: "★" priority, "in Xs" schedule
                suffix_parts = []
                if e.priority > 0:
                    suffix_parts.append("★")
                if e.dispatch_at is not None:
                    suffix_parts.append(_scheduler.humanize_delta(e.dispatch_at))
                else:
                    suffix_parts.append("ASAP")
                suffix = "  " + " · ".join(suffix_parts)
                max_prev = cols - 8 - len(suffix)
                if max_prev < 10:
                    max_prev = 10
                if len(preview) > max_prev:
                    preview = preview[:max_prev - 3] + "..."
                line = f"    {i:>2}. {preview}{suffix}"
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
        if dispatch_hint:
            hint = "  " + dispatch_hint
            add("\x1b[36m║\x1b[0m\x1b[2m" + hint + "\x1b[0m"
                + " " * max(0, cols - 2 - len(hint)) + "\x1b[36m║\x1b[0m")
        if note:
            hint = "  " + note
            add("\x1b[36m║\x1b[0m\x1b[33m" + hint + "\x1b[0m"
                + " " * max(0, cols - 2 - len(hint)) + "\x1b[36m║\x1b[0m")

        # Dropdown autocomplete (when user is typing a /command name)
        if self._dropdown_active and self._dropdown_items:
            add("\x1b[36m║" + " " * (cols - 2) + "║\x1b[0m")
            hdr = "  \x1b[1;36mCommands\x1b[0m  (↑↓ select, Tab/Enter pick, Esc close)"
            # account for ANSI codes in length calc
            visible = "  Commands  (↑↓ select, Tab/Enter pick, Esc close)"
            pad = cols - 2 - len(visible)
            add("\x1b[36m║\x1b[0m" + hdr + " " * max(0, pad) + "\x1b[36m║\x1b[0m")
            for i, item in enumerate(self._dropdown_items):
                is_sel = (i == self._dropdown_selected)
                marker = "►" if is_sel else " "
                label = f"    {marker} {item['template']:<44}  {item['summary']}"
                if len(label) > cols - 4:
                    label = label[: cols - 7] + "..."
                pad = cols - 2 - len(label)
                if is_sel:
                    add("\x1b[36m║\x1b[0m\x1b[1;43;30m" + label
                        + " " * max(0, pad) + "\x1b[0m\x1b[36m║\x1b[0m")
                else:
                    add("\x1b[36m║\x1b[0m" + label
                        + " " * max(0, pad) + "\x1b[36m║\x1b[0m")

        # empty spacer row ABOVE the input line
        add("\x1b[36m║" + " " * (cols - 2) + "║\x1b[0m")

        # === INPUT LINE — remember its row so we can park cursor here ===
        input_row = row + 1  # ANSI rows are 1-based
        buf = "".join(self._queue_buf)
        prompt_visible = "  > "          # 4 cols: "  ", ">", " "
        # Keep the RIGHT side of buf visible if it overflows. Truncate by
        # visual width, not char count, so CJK characters (2 cols each) don't
        # drive the wrap point wrong. Track how many leading chars we dropped
        # so we can adjust cursor_pos accordingly.
        max_buf_visible_cols = cols - 2 - len(prompt_visible) - 1
        buf_display = buf
        dropped_from_left = 0
        while _visual_width(buf_display) > max_buf_visible_cols:
            buf_display = buf_display[1:]
            dropped_from_left += 1
        buf_vw = _visual_width(buf_display)
        line_text = (
            "\x1b[36m║\x1b[0m"                      # left border (no width)
            + "  "                                   # 2-col indent
            + "\x1b[1;35m>\x1b[0m "                  # coloured prompt + space
            + buf_display
        )
        visible_len = 2 + 2 + buf_vw                 # "  " + "> " + buf (visual)
        pad = cols - 2 - visible_len
        add(line_text + " " * max(0, pad) + "\x1b[36m║\x1b[0m")

        # cursor column: "║" (col 1) + "  " (2 cols) + "> " (2 cols)
        # + visual width of buf chars BEFORE cursor_pos (not the whole buf,
        # so the cursor lands mid-text when user moves it).
        cursor_visible_pos = max(0, self._cursor_pos - dropped_from_left)
        chars_before_cursor = buf_display[:cursor_visible_pos]
        cursor_col = 1 + 2 + 2 + _visual_width(chars_before_cursor) + 1

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

    # ------------------------- dropdown autocomplete -------------------------

    def _refresh_dropdown(self) -> None:
        """Recompute dropdown visibility from current buffer contents.

        Rules:
          - Only active when the first char is '/' and the buffer contains
            no space yet (i.e., user is still typing the command name).
          - Filter COMMANDS by prefix match.
          - Keep selection index in bounds.
        """
        buf = "".join(self._queue_buf)
        if buf.startswith("/") and " " not in buf:
            self._dropdown_items = _slash.filter_commands(buf)
            self._dropdown_active = bool(self._dropdown_items)
            if self._dropdown_selected >= max(1, len(self._dropdown_items)):
                self._dropdown_selected = 0
        else:
            self._dropdown_active = False
            self._dropdown_items = []
            self._dropdown_selected = 0

    def _apply_dropdown_selection(self) -> None:
        """Replace buffer with the currently selected command template,
        close the dropdown, and keep the user in queue mode to type args.
        """
        if not self._dropdown_items:
            return
        item = self._dropdown_items[self._dropdown_selected]
        name = item["name"]
        has_args = "<" in item["template"]
        new_buf = name + (" " if has_args else "")
        self._queue_buf = list(new_buf)
        # Park cursor at the END of the inserted template so the user
        # can start typing args immediately (previous bug: cursor stayed
        # at wherever the `/` was when dropdown opened, e.g. pos 1, so
        # the display looked like `> /│at ` instead of `> /at │`).
        self._cursor_pos = len(self._queue_buf)
        self._dropdown_active = False
        self._dropdown_items = []
        self._update_input_line()

    # ------------------------- PTY write wrapper -------------------------

    def _send_to_pty(self, data: bytes) -> None:
        try:
            self.pty_write(data)
        except Exception as e:
            sys.stdout.write(f"\r\n\x1b[31m[claude -q] pty write error: {e}\x1b[0m\r\n")
            sys.stdout.flush()

    def _dispatch_hint(self, pending: list) -> str:
        """Build a one-line hint for the queue UI explaining why (or when)
        the next entry will dispatch. Read from the monitor's status.json
        snapshot so we can surface the real reason to the user.
        """
        if not pending:
            return ""

        # next entry in dispatch order (pending is already sorted by caller)
        head = pending[0]

        # If head is scheduled for the future, show the countdown.
        if head.dispatch_at is not None:
            delta = _scheduler.humanize_delta(head.dispatch_at)
            if delta.startswith("in "):
                return f"Next: {head.text[:30]!r} fires {delta}"
            if delta.startswith("overdue "):
                return (f"Next: {head.text[:30]!r} is overdue — waiting "
                        f"for Claude idle")

        # ASAP entry: show the real blocker from monitor's last tick
        try:
            status_path = self.queue_path.parent / "status.json"
            if status_path.exists():
                import json as _json
                data = _json.loads(status_path.read_text(encoding="utf-8"))
                reasons = data.get("last_reasons") or {}
                if data.get("idle"):
                    return "Next: ASAP — Claude is idle, dispatching soon"
                blockers = []
                if reasons.get("prompt_visible") is False:
                    blockers.append("Claude's input has draft text "
                                    "(submit or clear it)")
                if reasons.get("not_busy") is False:
                    blockers.append("Claude is busy")
                if reasons.get("stable") is False:
                    blockers.append("Claude output still changing")
                if blockers:
                    return "Waiting: " + "; ".join(blockers)
        except Exception:
            pass
        return "Next: ASAP (waiting for Claude to reach empty prompt)"

    @staticmethod
    def _set_title(text: str) -> None:
        """Write the window title via OSC 0 so confirmation messages don't
        pollute Claude's main screen. Silently fail on terminals that don't
        support title setting (the status_bar thread also sets it so
        this is only a short-lived override; it gets refreshed every
        ~250 ms anyway)."""
        try:
            payload = f"\x1b]0;{text}\x07".encode("utf-8", errors="replace")
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        except Exception:
            pass

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
