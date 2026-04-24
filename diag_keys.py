"""diag_keys.py - diagnose what msvcrt.getwch() returns for each key.

Run this in the SAME PowerShell/Windows Terminal you use for claude-q.
Press keys - it will print each key's hex code and name. Press Ctrl+C to exit.
"""
from __future__ import annotations

import sys
import time

try:
    import msvcrt
except ImportError:
    print("msvcrt not available; this tool is Windows-only.")
    sys.exit(1)

print("=" * 60)
print("claude-q 鍵盤診斷工具")
print("=" * 60)
print("按下以下按鍵，觀察 hex code：")
print("  1. Enter   (應該看到 \\r = 0x0d)")
print("  2. 左方向鍵 (應該看到 0xe0 + K = 0x4b)")
print("  3. 右方向鍵 (應該看到 0xe0 + M = 0x4d)")
print("  4. Esc     (應該看到 \\x1b = 0x1b)")
print("  5. Ctrl+Q  (應該看到 \\x11)")
print("  6. 英文字母 a")
print("  7. Ctrl+C 結束")
print("=" * 60)
print()

try:
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            # log details
            codes = [hex(ord(c)) for c in ch]
            readable = repr(ch)
            msg = f"KEY: {readable:>12}  codes={codes}"
            # if special prefix, read next char
            if ch in ("\x00", "\xe0"):
                scan = msvcrt.getwch()
                msg += f"  |  SCAN: {scan!r} codes={[hex(ord(c)) for c in scan]}"
            print(msg, flush=True)
            if ch == "\x03":  # Ctrl+C
                print("exiting...")
                break
        else:
            time.sleep(0.01)
except KeyboardInterrupt:
    print("\nexiting (KeyboardInterrupt)")
