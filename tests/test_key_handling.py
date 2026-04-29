"""Unit tests for TerminalRelay._handle_key().

Covers the Shift+Tab and Alt+letter fixes plus regression coverage for
the existing key handlers (plain Tab, Ctrl+letter, Enter, Backspace,
arrow VT passthrough, queue-mode buffering).

Strategy
--------
We bypass the thread/console layer and call _handle_key() directly with
a stub `Key` namedtuple matching win_console_input.Key. PTY writes are
captured into a list via a fake pty_write callback; we then assert on
the exact bytes that would have reached Claude.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Force-import even if win_console_input fails (no real CONIN in pytest).
import terminal_relay as tr  # noqa: E402


# ------------------------- helpers -------------------------

@dataclass
class FakeKey:
    """Mirror of win_console_input.Key, usable without Windows console."""
    text: str = ""
    vkey: int = 0
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    vt: Optional[str] = None


def make_relay():
    """Construct a TerminalRelay whose pty_write captures bytes."""
    sent: list[bytes] = []

    def pty_write(b: bytes) -> int:
        sent.append(b)
        return len(b)

    relay = tr.TerminalRelay(
        queue_path=Path("/tmp/nonexistent.jsonl"),  # unused in these tests
        pty_write=pty_write,
        toggle_vk=tr.VK_Q,
    )
    return relay, sent


def vt_for(vk: int) -> str:
    """Look up the VT sequence the real win_console_input would attach."""
    try:
        import win_console_input as wci
        return wci._VK_TO_VT.get(vk)
    except Exception:
        # Fallback: replicate the relevant entries we test against.
        return {
            0x25: "\x1b[D",
            0x26: "\x1b[A",
            0x27: "\x1b[C",
            0x28: "\x1b[B",
        }.get(vk)


# ============================================================
# THE FIXES — Shift+Tab and Alt+letter
# ============================================================

class TestShiftTab:
    """Bug 1 fix: Shift+Tab should send CSI Z (\\x1b[Z), not \\t."""

    def test_plain_tab_direct_sends_horizontal_tab(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_TAB))
        assert sent == [b"\t"], f"plain Tab should send \\t, got {sent!r}"

    def test_shift_tab_direct_sends_csi_z(self):
        """The whole point of the fix — Claude Code's mode toggle."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_TAB, shift=True))
        assert sent == [b"\x1b[Z"], (
            f"Shift+Tab MUST send \\x1b[Z (CSI Z back-tab), got {sent!r}"
        )

    def test_tab_in_queue_mode_does_not_reach_pty(self):
        """Queue mode should swallow Tab (used for dropdown / commit)."""
        relay, sent = make_relay()
        relay._mode = "queue"
        relay._handle_key(FakeKey(vkey=tr.VK_TAB))
        assert sent == [], f"queue-mode Tab must not reach PTY, got {sent!r}"

    def test_shift_tab_in_queue_mode_does_not_reach_pty(self):
        relay, sent = make_relay()
        relay._mode = "queue"
        relay._handle_key(FakeKey(vkey=tr.VK_TAB, shift=True))
        assert sent == [], (
            f"queue-mode Shift+Tab must not reach PTY, got {sent!r}"
        )


class TestAltLetter:
    """Bug 2 fix: Alt+letter should send ESC + letter (meta-prefix)."""

    def test_alt_v_sends_esc_v(self):
        """Alt+V is what Claude Code Ink TUI listens for to paste image."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x56, alt=True))  # 0x56 = 'V'
        assert sent == [b"\x1bv"], (
            f"Alt+V must send b'\\x1bv', got {sent!r}"
        )

    def test_alt_v_with_text_payload_still_sends_esc_v(self):
        """Regression: image-paste broken under claude-q.

        On some Windows keyboard layouts / IME states, ReadConsoleInputW
        reports Alt+V with `UnicodeChar='v'`, so the Key arrives with
        text='v' AND alt=True. Previously the plain-character branch ran
        first and sent bare b'v' (no ESC prefix), so Claude Code's Ink
        TUI saw a literal 'v' typed into the input box and image paste
        never triggered. The Alt+letter handler must take priority.
        """
        relay, sent = make_relay()
        relay._handle_key(FakeKey(text="v", vkey=0x56, alt=True))
        assert sent == [b"\x1bv"], (
            f"Alt+V (with text='v') must STILL send b'\\x1bv' so "
            f"Claude's image-paste binding fires, got {sent!r}"
        )

    def test_alt_m_sends_esc_m(self):
        """Spot-check another letter to confirm general handler."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x4D, alt=True))  # 'M'
        assert sent == [b"\x1bm"], (
            f"Alt+M must send b'\\x1bm', got {sent!r}"
        )

    def test_alt_a_sends_esc_a(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x41, alt=True))  # 'A'
        assert sent == [b"\x1ba"]

    def test_alt_z_sends_esc_z(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x5A, alt=True))  # 'Z'
        assert sent == [b"\x1bz"]

    def test_alt_letter_in_queue_mode_does_not_reach_pty(self):
        relay, sent = make_relay()
        relay._mode = "queue"
        relay._handle_key(FakeKey(vkey=0x56, alt=True))  # Alt+V in queue
        assert sent == [], (
            f"queue-mode Alt+V must not reach PTY, got {sent!r}"
        )

    def test_ctrl_alt_letter_does_not_double_fire(self):
        """Ctrl+Alt+V should match NEITHER Ctrl+letter nor Alt+letter
        (we explicitly excluded the overlap to avoid sending two payloads)."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x56, ctrl=True, alt=True))
        assert sent == [], (
            f"Ctrl+Alt+V should fall through (no duplicate fire), got {sent!r}"
        )


# ============================================================
# REGRESSION — existing handlers must still work
# ============================================================

class TestRegressionCtrlLetter:
    """Existing Ctrl+letter handler shouldn't break after the fix."""

    def test_ctrl_c_sends_etx(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_C, ctrl=True))
        assert sent == [b"\x03"]

    def test_ctrl_v_sends_syn(self):
        """Ctrl+V = 0x16 (SYN). On Linux/Mac native Claude this is the
        actual paste binding."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x56, ctrl=True))
        assert sent == [b"\x16"]

    def test_ctrl_a_sends_soh(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x41, ctrl=True))
        assert sent == [b"\x01"]

    def test_ctrl_z_sends_sub(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=0x5A, ctrl=True))
        assert sent == [b"\x1a"]


class TestRegressionPlainCharacters:
    def test_letter_a_sent_as_utf8(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(text="a", vkey=0x41))
        assert sent == [b"a"]

    def test_chinese_char_sent_as_utf8(self):
        """IME-committed Chinese must round-trip correctly."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(text="中", vkey=0))
        assert sent == ["中".encode("utf-8")]

    def test_plain_char_in_queue_mode_buffered_not_sent(self):
        relay, sent = make_relay()
        relay._mode = "queue"
        relay._handle_key(FakeKey(text="a", vkey=0x41))
        assert sent == []
        assert relay._queue_buf == ["a"]
        assert relay._cursor_pos == 1


class TestRegressionEnter:
    def test_plain_enter_direct_sends_cr_by_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_Q_ENTER", raising=False)
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))
        assert sent == [b"\r"]

    def test_shift_enter_direct_sends_lf(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, shift=True))
        assert sent == [b"\n"]

    def test_alt_enter_direct_sends_lf(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, alt=True))
        assert sent == [b"\n"]

    def test_ctrl_enter_direct_sends_lf(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, ctrl=True))
        assert sent == [b"\n"]


class TestRegressionEditingKeys:
    def test_backspace_direct_sends_del(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_BACK))
        assert sent == [b"\x7f"]

    def test_delete_direct_sends_csi_3_tilde(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_DELETE, vt="\x1b[3~"))
        assert sent == [b"\x1b[3~"]


class TestRegressionArrowVT:
    """Arrow keys carry vt= and should be forwarded as-is in direct mode."""

    @pytest.mark.parametrize("vk,vt", [
        (tr.VK_LEFT, "\x1b[D"),
        (tr.VK_RIGHT, "\x1b[C"),
        (tr.VK_UP, "\x1b[A"),
        (tr.VK_DOWN, "\x1b[B"),
    ])
    def test_arrow_keys_passthrough(self, vk, vt):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=vk, vt=vt))
        assert sent == [vt.encode()]


class TestRegressionCtrlC:
    """Ctrl+C must always reach the PTY (forwarded for child interrupt)."""

    def test_ctrl_c_in_direct_mode(self):
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_C, ctrl=True))
        assert sent == [b"\x03"]

    def test_ctrl_c_in_queue_mode_cancels_buffer_and_forwards(self):
        relay, sent = make_relay()
        relay._mode = "queue"
        relay._queue_buf = ["h", "i"]
        relay._cursor_pos = 2
        relay._handle_key(FakeKey(vkey=tr.VK_C, ctrl=True))
        # buffer cleared
        assert relay._queue_buf == []
        # Ctrl+C still forwarded
        assert sent == [b"\x03"]


# ============================================================
# Integration-ish: full sequence simulation
# ============================================================

class TestKeystrokeSequences:
    """Multi-key sequences that catch regressions across handlers."""

    def test_type_then_shift_tab_then_alt_v(self):
        """Realistic workflow: type 'h', press Shift+Tab to switch mode,
        then Alt+V to trigger paste-image. All three should reach PTY."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(text="h", vkey=0x48))
        relay._handle_key(FakeKey(vkey=tr.VK_TAB, shift=True))
        relay._handle_key(FakeKey(vkey=0x56, alt=True))
        assert sent == [b"h", b"\x1b[Z", b"\x1bv"]

    def test_unmodified_tab_unaffected_by_alt_handler(self):
        """Make sure my new Alt+letter branch doesn't accidentally swallow
        plain Tab (different VK, but defensive check)."""
        relay, sent = make_relay()
        relay._handle_key(FakeKey(vkey=tr.VK_TAB))
        relay._handle_key(FakeKey(vkey=tr.VK_TAB, shift=True))
        relay._handle_key(FakeKey(vkey=tr.VK_TAB))
        assert sent == [b"\t", b"\x1b[Z", b"\t"]


# ============================================================
# Paste-burst handling — multi-line clipboard pastes must not
# fire Enter mid-stream (Windows Terminal converts Ctrl+V into a
# stream of synthetic keystrokes; embedded `\r` chars used to
# submit Claude's input prematurely, truncating the paste).
# ============================================================

class TestPasteBurst:
    """v0.4.10 fix: VK_RETURN inside a sub-_PASTE_GAP_S keystroke
    burst is treated as a literal newline, not a submit."""

    def test_paste_burst_with_embedded_return_does_not_submit(self):
        """Simulate Windows-Terminal-driven paste of `ab\\r\\ncd`.
        The relay must produce the entire content as input (with the
        \\r becoming a literal newline) — never the bare \\r that would
        submit Claude's input mid-paste."""
        import time
        relay, sent = make_relay()

        # First key arrives "fresh" (gap from 0 is large)
        relay._handle_key(FakeKey(text="a", vkey=0x41))
        # Subsequent keys arrive immediately — paste-burst behavior.
        # Our threshold is 5ms; we deliberately backdate _last_key_time
        # to make the gap effectively zero on each call.
        for ch, vk in [("b", 0x42), ("\r", tr.VK_RETURN),
                       ("\n", 0), ("c", 0x43), ("d", 0x44)]:
            relay._last_key_time = time.monotonic()  # simulate <5ms gap
            relay._handle_key(FakeKey(text=ch, vkey=vk))

        # The bytes the PTY received: every one of these is a paste
        # continuation, so the \r becomes \n and the rest are literal.
        # Crucially: NO bare b"\r" anywhere — that would have submitted
        # Claude's input.
        assert b"\r" not in b"".join(sent), (
            f"paste burst leaked a bare \\r (would submit mid-paste): "
            f"{sent!r}"
        )
        # And the actual content reached Claude in order
        assert b"".join(sent) == b"ab\n\ncd"

    def test_human_enter_after_typing_still_submits(self):
        """A normal pause-then-Enter must still submit (gap > _PASTE_GAP_S).
        We sleep for slightly longer than the paste threshold to simulate
        a user finishing a thought and pressing Enter."""
        import time
        relay, sent = make_relay()

        # Type "hi" then a real human Enter (50ms gap = way past 5ms).
        relay._handle_key(FakeKey(text="h", vkey=0x48))
        relay._handle_key(FakeKey(text="i", vkey=0x49))
        time.sleep(0.05)
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))

        assert sent == [b"h", b"i", b"\r"], (
            f"plain Enter after typing should submit (\\r), got {sent!r}"
        )

    def test_modifier_enter_still_sends_newline_regardless_of_burst(self):
        """Ctrl/Shift/Alt + Enter has always meant "literal newline";
        the new paste-burst logic must not change that behavior even when
        the gap from the previous key is large."""
        import time
        relay, sent = make_relay()
        time.sleep(0.05)
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, ctrl=True))
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, shift=True))
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN, alt=True))
        assert sent == [b"\n", b"\n", b"\n"]

    def test_queue_mode_paste_appends_newline_not_commit(self):
        """In queue mode, a normal Enter commits the buffer to queue.jsonl.
        Inside a paste burst, embedded \\r must instead append a literal
        newline to the queue input buffer so the whole paste lands as
        one queued entry."""
        import time
        relay, sent = make_relay()
        # Toggle into queue mode
        relay._mode = "queue"
        relay._queue_buf = []
        relay._cursor_pos = 0

        # Track _commit_queue_input — it should NOT be called during a paste.
        commits = []
        orig_commit = relay._commit_queue_input
        relay._commit_queue_input = lambda: commits.append(True)

        # Stub out _update_input_line — it depends on terminal state we
        # don't have here.
        relay._update_input_line = lambda: None
        relay._refresh_dropdown = lambda: None

        # Simulate paste of "ab\rcd"
        relay._handle_key(FakeKey(text="a", vkey=0x41))
        for ch, vk in [("b", 0x42), ("\r", tr.VK_RETURN), ("c", 0x43), ("d", 0x44)]:
            relay._last_key_time = time.monotonic()
            relay._handle_key(FakeKey(text=ch, vkey=vk))

        assert commits == [], (
            "paste burst must not commit queue input; got commits"
        )
        assert "".join(relay._queue_buf) == "ab\ncd", (
            f"paste should land in buffer with \\n inserted; "
            f"got {relay._queue_buf!r}"
        )

        # Restore
        relay._commit_queue_input = orig_commit

    def test_ime_chinese_commit_burst_is_not_misclassified_as_paste(self):
        """Chinese IME commit can deliver multiple characters back-to-back
        when a candidate phrase is selected (e.g. typing `今天天氣` via
        Bopomofo IME commits the 4-char phrase as one event followed
        rapidly by the next Enter for submission). The paste-burst logic
        only fires for VK_RETURN inside a burst — plain CJK chars must
        still flow through identically to typed input regardless of gap."""
        import time
        relay, sent = make_relay()

        # Simulate IME committing 4 Chinese chars in rapid succession
        # followed by a paused human Enter to submit.
        for ch in "今天天氣":
            relay._last_key_time = time.monotonic()
            relay._handle_key(FakeKey(text=ch))
        # Human pause then Enter (50ms gap > 5ms threshold)
        time.sleep(0.05)
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))

        # All 4 chars sent as UTF-8, then a real submit \r — IME burst
        # did NOT interfere with the human Enter's interpretation.
        assert sent[-1] == b"\r", (
            f"human Enter after IME burst must still submit; got "
            f"sent[-1]={sent[-1]!r}"
        )
        assert b"".join(sent[:-1]).decode("utf-8") == "今天天氣"

    def test_fast_typist_at_60wpm_does_not_trigger_paste_detection(self):
        """A fast human typist reaches roughly 60-100 WPM, which is one
        keystroke every ~30-50ms. Our 5ms threshold must comfortably
        exclude that range — a Real Enter at the end of a typed word
        must always submit."""
        import time
        relay, sent = make_relay()

        # 80 WPM ≈ 30ms/char. Type "fast" with realistic gaps.
        relay._handle_key(FakeKey(text="f", vkey=0x46))
        for ch, vk in [("a", 0x41), ("s", 0x53), ("t", 0x54)]:
            time.sleep(0.030)
            relay._handle_key(FakeKey(text=ch, vkey=vk))
        time.sleep(0.030)
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))

        assert sent == [b"f", b"a", b"s", b"t", b"\r"], (
            f"fast-typist sequence must not trigger paste-burst; "
            f"got {sent!r}"
        )


# ============================================================
# Native-claude byte-equivalence — verify our relay sends EXACTLY
# the same bytes that raw claude.exe would receive if you typed
# directly into it. Any divergence means a feature break.
# ============================================================

class TestNativeClaudeByteEquivalence:
    """For each common keypress, our relay's output must match what a
    raw terminal-to-claude.exe pipe would produce. If a future change
    accidentally munges any of these, claude.exe will misbehave even
    though every individual handler test still passes."""

    def test_chinese_paste_via_ime_yields_exact_utf8(self):
        """Pasting Chinese text via IME or clipboard must produce the
        same UTF-8 bytes a native terminal would deliver. We verify
        that paste-burst handling does NOT mutate non-VK_RETURN chars."""
        import time
        relay, sent = make_relay()

        # Paste burst of "你好世界" (each char as a separate keystroke,
        # which is how Windows Terminal delivers paste).
        relay._handle_key(FakeKey(text="你"))
        for ch in "好世界":
            relay._last_key_time = time.monotonic()
            relay._handle_key(FakeKey(text=ch))

        assert b"".join(sent) == "你好世界".encode("utf-8"), (
            "paste-burst handling must not mutate CJK chars"
        )

    def test_arrow_key_burst_passes_through_unchanged(self):
        """A user holding an arrow key (or paste-induced auto-cursor-move)
        delivers VT sequences in rapid bursts. All must pass through
        unchanged — paste-burst only special-cases VK_RETURN."""
        import time
        relay, sent = make_relay()

        relay._handle_key(FakeKey(vkey=0x27, vt="\x1b[C"))  # right
        for vk, vt in [(0x27, "\x1b[C"), (0x25, "\x1b[D"), (0x28, "\x1b[B")]:
            relay._last_key_time = time.monotonic()
            relay._handle_key(FakeKey(vkey=vk, vt=vt))

        # All 4 arrows pass through, none mangled
        assert sent == [b"\x1b[C", b"\x1b[C", b"\x1b[D", b"\x1b[B"]

    def test_alt_v_inside_paste_still_sends_esc_v(self):
        """Alt+V is the Claude image-paste shortcut (the original v0.4.x
        fix). Even if it somehow arrives during a paste burst, the
        paste-burst logic ONLY targets VK_RETURN, so Alt+V must still
        produce ESC+v."""
        import time
        relay, sent = make_relay()

        relay._handle_key(FakeKey(text="x", vkey=0x58))
        relay._last_key_time = time.monotonic()
        relay._handle_key(FakeKey(vkey=0x56, alt=True, text="v"))

        assert sent == [b"x", b"\x1bv"], (
            f"Alt+V must still send ESC+v even mid-burst; got {sent!r}"
        )

    def test_ctrl_c_during_paste_still_interrupts(self):
        """Pasted content rarely contains Ctrl+C as a virtual key event,
        but if a user genuinely needs to interrupt mid-paste they can
        press Ctrl+C and it must reach Claude (\\x03)."""
        import time
        relay, sent = make_relay()

        relay._handle_key(FakeKey(text="a", vkey=0x41))
        relay._last_key_time = time.monotonic()
        # Burst-timed Ctrl+C must still send \x03 (interrupt), not \x16
        relay._handle_key(FakeKey(vkey=tr.VK_C, ctrl=True))

        assert sent == [b"a", b"\x03"]


# ============================================================
# Combined stress: paste containing every "dangerous" character
# (Enter, Tab, control chars, CJK, ASCII punct, escape sequences)
# must arrive at the PTY in original textual form. The whole
# point of the v0.4.10 fix is that this becomes safe.
# ============================================================

class TestRealWorldPasteContent:
    def test_pasting_python_traceback_lands_intact(self):
        """Real-world payload: a Python traceback. This is exactly what
        the user pasted when the original bug was reported. Every
        character must reach Claude in order; no submission until the
        user explicitly hits Enter after the burst ends."""
        import time
        relay, sent = make_relay()

        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "foo.py", line 42, in <module>\n'
            "    raise ValueError('bad')\n"
            "ValueError: bad\n"
        )

        # Deliver as paste (each char back-to-back). For \r and \n in
        # the source, simulate Windows Terminal's paste machinery: \r
        # arrives as VK_RETURN, \n arrives as text='\n', vkey=0.
        relay._handle_key(FakeKey(text=traceback_text[0]))
        for i, ch in enumerate(traceback_text[1:], 1):
            relay._last_key_time = time.monotonic()
            if ch == "\r":
                relay._handle_key(FakeKey(text=ch, vkey=tr.VK_RETURN))
            elif ch == "\n":
                # \n typically arrives as text-only in Windows console
                relay._handle_key(FakeKey(text=ch))
            else:
                relay._handle_key(FakeKey(text=ch))

        joined = b"".join(sent).decode("utf-8")
        # No premature submit in the middle of the paste
        assert "\r" not in joined, "paste leaked a bare \\r mid-stream"
        # The full content is preserved (all newlines as \n)
        assert joined == traceback_text.replace("\r", "\n")

    def test_paste_then_user_edits_then_submits(self):
        """Paste workflow: user pastes a partial message, edits the end,
        then hits Enter. The Enter MUST submit (gap from paste-end to
        user's typing-end is large)."""
        import time
        relay, sent = make_relay()

        # Burst paste "hello\n"
        relay._handle_key(FakeKey(text="h"))
        for ch, vk in [("e", 0), ("l", 0), ("l", 0), ("o", 0)]:
            relay._last_key_time = time.monotonic()
            relay._handle_key(FakeKey(text=ch, vkey=vk))
        # Embedded newline (paste continuation)
        relay._last_key_time = time.monotonic()
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))
        # User pauses, types " more", hits Enter
        time.sleep(0.05)
        for ch in " more":
            relay._handle_key(FakeKey(text=ch))
            time.sleep(0.05)
        relay._handle_key(FakeKey(vkey=tr.VK_RETURN))

        joined = b"".join(sent).decode("utf-8")
        # The pasted Enter became \n; the human Enter at the end is \r
        assert joined.endswith("\r"), (
            f"final human Enter must submit (\\r); got tail "
            f"{joined[-5:]!r}"
        )
        assert "hello\n" in joined  # paste preserved as one line of input
