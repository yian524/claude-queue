"""win_console_input.py - Direct Windows console input via ReadConsoleInputW.

Why not msvcrt.getwch
---------------------
msvcrt reads from the console's cooked/line stream, which is affected by:
  1. IME composition mode - IME eats Enter to confirm composition
  2. Windows Terminal ANSI response injection - DA/CPR replies arrive via
     stdin and look indistinguishable from user input
  3. Wide-char vs narrow-char mixing pitfalls

ReadConsoleInputW reads INPUT_RECORD structs directly from CONIN$ and lets
us filter for KEY_EVENT with bKeyDown=True. Synthetic console input (like
Windows Terminal's DA query replies) comes through as different event types
we can ignore.

We expose an API compatible with the msvcrt-based loop:
  - kbhit()  -> bool
  - getkey() -> (ch: str, vkey: int, scan: int, ctrl: bool, alt: bool, shift: bool)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass
from typing import Optional

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

STD_INPUT_HANDLE = wt.DWORD(-10 & 0xFFFFFFFF)

# Event types
KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004
MENU_EVENT = 0x0008
FOCUS_EVENT = 0x0010

# Control key states
LEFT_ALT_PRESSED = 0x0002
RIGHT_ALT_PRESSED = 0x0001
LEFT_CTRL_PRESSED = 0x0008
RIGHT_CTRL_PRESSED = 0x0004
SHIFT_PRESSED = 0x0010

# Console modes
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


class KEY_EVENT_RECORD(ctypes.Structure):
    class _Char(ctypes.Union):
        _fields_ = [("UnicodeChar", wt.WCHAR), ("AsciiChar", ctypes.c_char)]

    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", _Char),
        ("dwControlKeyState", wt.DWORD),
    ]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition_X", wt.SHORT),
        ("dwMousePosition_Y", wt.SHORT),
        ("dwButtonState", wt.DWORD),
        ("dwControlKeyState", wt.DWORD),
        ("dwEventFlags", wt.DWORD),
    ]


class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    _fields_ = [("dwSize_X", wt.SHORT), ("dwSize_Y", wt.SHORT)]


class MENU_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwCommandId", wt.DWORD)]


class FOCUS_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bSetFocus", wt.BOOL)]


class _EventUnion(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("MouseEvent", MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD),
        ("MenuEvent", MENU_EVENT_RECORD),
        ("FocusEvent", FOCUS_EVENT_RECORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EventUnion)]


# function signatures
kernel32.GetStdHandle.restype = wt.HANDLE
kernel32.GetStdHandle.argtypes = [wt.DWORD]

kernel32.GetConsoleMode.restype = wt.BOOL
kernel32.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

kernel32.SetConsoleMode.restype = wt.BOOL
kernel32.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]

kernel32.GetNumberOfConsoleInputEvents.restype = wt.BOOL
kernel32.GetNumberOfConsoleInputEvents.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

kernel32.ReadConsoleInputW.restype = wt.BOOL
kernel32.ReadConsoleInputW.argtypes = [
    wt.HANDLE, ctypes.POINTER(INPUT_RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD)
]


# ============================= scan-code / VK -> VT sequence =============================

_VK_TO_VT = {
    0x25: "\x1b[D",  # VK_LEFT
    0x26: "\x1b[A",  # VK_UP
    0x27: "\x1b[C",  # VK_RIGHT
    0x28: "\x1b[B",  # VK_DOWN
    0x24: "\x1b[H",  # VK_HOME
    0x23: "\x1b[F",  # VK_END
    0x21: "\x1b[5~", # VK_PRIOR (PgUp)
    0x22: "\x1b[6~", # VK_NEXT  (PgDn)
    0x2D: "\x1b[2~", # VK_INSERT
    0x2E: "\x1b[3~", # VK_DELETE
    0x70: "\x1bOP",  # VK_F1
    0x71: "\x1bOQ",  # VK_F2
    0x72: "\x1bOR",  # VK_F3
    0x73: "\x1bOS",  # VK_F4
    0x74: "\x1b[15~",# VK_F5
    0x75: "\x1b[17~",# VK_F6
    0x76: "\x1b[18~",# VK_F7
    0x77: "\x1b[19~",# VK_F8
    0x78: "\x1b[20~",# VK_F9
    0x79: "\x1b[21~",# VK_F10
}


# ============================= API =============================

@dataclass
class Key:
    """A single keyboard event after filtering.

    text  - the UnicodeChar payload (may be empty for pure special keys)
    vkey  - Windows virtual key code
    ctrl  - Ctrl held
    alt   - Alt held
    shift - Shift held
    vt    - pre-computed VT sequence to send to PTY (None if just use text)
    """
    text: str
    vkey: int
    ctrl: bool
    alt: bool
    shift: bool
    vt: Optional[str]


class ConsoleInput:
    """Context-manager wrapper that puts CONIN$ in a sane input mode."""

    def __init__(self, enable_mouse: bool = False):
        self._h = None
        self._saved_mode = wt.DWORD(0)
        self._enable_mouse = enable_mouse

    def __enter__(self) -> "ConsoleInput":
        self._h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not self._h or self._h == wt.HANDLE(-1).value:
            raise OSError("GetStdHandle failed")
        # save current mode
        if not kernel32.GetConsoleMode(self._h, ctypes.byref(self._saved_mode)):
            raise ctypes.WinError(ctypes.get_last_error())
        # raw mode: disable line / echo / processed-input (so Ctrl+C doesn't
        # kill our process), keep window-size events, optionally mouse
        new_mode = ENABLE_WINDOW_INPUT
        if self._enable_mouse:
            new_mode |= ENABLE_MOUSE_INPUT
        kernel32.SetConsoleMode(self._h, wt.DWORD(new_mode))
        return self

    def __exit__(self, *exc) -> None:
        if self._h:
            kernel32.SetConsoleMode(self._h, self._saved_mode)

    # ------------------- poll -------------------

    def has_input(self) -> bool:
        n = wt.DWORD(0)
        if not kernel32.GetNumberOfConsoleInputEvents(self._h, ctypes.byref(n)):
            return False
        return n.value > 0

    def read_key(self, timeout_s: float = 0.02) -> Optional[Key]:
        """Read one keyboard event. Returns None on timeout or non-key events."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.has_input():
                time.sleep(0.003)
                continue
            rec = INPUT_RECORD()
            read = wt.DWORD(0)
            ok = kernel32.ReadConsoleInputW(
                self._h, ctypes.byref(rec), 1, ctypes.byref(read))
            if not ok or read.value == 0:
                return None
            if rec.EventType != KEY_EVENT:
                # mouse/focus/window-size events - skip
                continue
            ke = rec.Event.KeyEvent
            if not ke.bKeyDown:
                continue  # only process key-down
            return self._to_key(ke)
        return None

    @staticmethod
    def _to_key(ke: KEY_EVENT_RECORD) -> Optional[Key]:
        vk = ke.wVirtualKeyCode
        cs = ke.dwControlKeyState
        ctrl = bool(cs & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED))
        alt = bool(cs & (LEFT_ALT_PRESSED | RIGHT_ALT_PRESSED))
        shift = bool(cs & SHIFT_PRESSED)
        text = ke.uChar.UnicodeChar
        vt = _VK_TO_VT.get(vk)
        # Skip pure modifier keys (Shift/Ctrl/Alt by themselves)
        if vk in (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
            return None
        return Key(text=text or "", vkey=vk, ctrl=ctrl, alt=alt, shift=shift, vt=vt)


# ============================= self-test =============================

def _self_test() -> int:
    """Interactive test: press keys and see decoded events."""
    print("=" * 60)
    print("win_console_input 自我測試")
    print("=" * 60)
    print("按下以下按鍵（Ctrl+C 結束）：")
    print("  1. Enter")
    print("  2. 左右方向鍵")
    print("  3. 英文字母")
    print("  4. 中文字 (IME 開著打一個字)")
    print("  5. Esc")
    print("=" * 60)
    with ConsoleInput() as ci:
        while True:
            k = ci.read_key(timeout_s=1.0)
            if k is None:
                continue
            if k.ctrl and k.vkey == 0x43:  # Ctrl+C
                print("Ctrl+C detected, exiting")
                break
            mods = []
            if k.ctrl: mods.append("C")
            if k.alt: mods.append("A")
            if k.shift: mods.append("S")
            mod = "+".join(mods) if mods else "-"
            tr = repr(k.text) if k.text else "(none)"
            vt = repr(k.vt) if k.vt else "-"
            print(f"vkey=0x{k.vkey:02X}  mod={mod:5}  text={tr:10}  vt={vt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
