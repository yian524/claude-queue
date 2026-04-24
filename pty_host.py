"""pty_host.py — thin wrapper around pywinpty's PtyProcess.

Responsibilities
----------------
* spawn(cmd, cols, rows)      -> start subprocess attached to a pseudo-terminal
* read_nonblocking(timeout_s) -> return whatever bytes the child emitted
* write(data)                 -> write bytes to child's stdin
* resize(cols, rows)          -> adjust terminal dims (on SIGWINCH / WT resize)
* is_alive / wait / terminate -> lifecycle

Why a thin wrapper: pywinpty's API is decent but idiosyncratic.
Isolating here makes terminal_relay / monitor testable via mock, and
lets us swap to ptyprocess on POSIX without rewriting call sites.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


try:
    import winpty  # pywinpty >= 2.x exposes this module name
    _HAS_WINPTY = True
except ImportError:
    winpty = None  # type: ignore[assignment]
    _HAS_WINPTY = False


@dataclass
class SpawnSpec:
    cmd: str                  # e.g. "claude" or "cmd.exe"
    cwd: Optional[str] = None
    cols: int = 120
    rows: int = 40
    env: Optional[dict] = None


class PtyHost:
    """Single child process attached to a pywinpty PTY.

    Threading model:
      - a reader thread pulls from the PTY and appends into an in-memory
        ring buffer `tail_buf` + emits every chunk via `on_data` callback.
      - callers read from `tail_buf` for idle detection, subscribe to
        `on_data` for live passthrough.
    """

    def __init__(self, spec: SpawnSpec, tail_chars: int = 4000):
        if not _HAS_WINPTY:
            raise RuntimeError("pywinpty (winpty) is not installed. "
                               "run: pip install pywinpty")
        self.spec = spec
        self._tail_capacity = tail_chars
        self._tail_buf: Deque[str] = deque()
        self._tail_len = 0
        self._lock = threading.Lock()
        self._proc: Optional["winpty.PtyProcess"] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._on_data_cb = None  # callable(bytes) -> None

    # ------------------------- lifecycle -------------------------

    def spawn(self) -> None:
        if self._proc is not None:
            raise RuntimeError("already spawned")
        # resolve command (support bare names by looking up PATH)
        cmd = self.spec.cmd
        exe = shutil.which(cmd) or cmd
        env = self.spec.env or dict(os.environ)
        # make sure UTF-8 comes through
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._proc = winpty.PtyProcess.spawn(
            exe,
            dimensions=(self.spec.rows, self.spec.cols),
            cwd=self.spec.cwd,
            env=env,
        )
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="pty-reader", daemon=True
        )
        self._reader_thread.start()

    def set_on_data(self, callback) -> None:
        """Register a callback called with each bytes chunk read from PTY.

        Used by terminal_relay to write passthrough to sys.stdout.
        """
        self._on_data_cb = callback

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.isalive()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self._proc is None:
            return None
        t0 = time.monotonic()
        while self._proc.isalive():
            if timeout is not None and (time.monotonic() - t0) >= timeout:
                return None
            time.sleep(0.05)
        try:
            return self._proc.exitstatus
        except Exception:
            return None

    def terminate(self, force: bool = False) -> None:
        self._stop.set()
        if self._proc is None:
            return
        try:
            self._proc.terminate(force=force)
        except Exception:
            pass

    # ------------------------- I/O -------------------------

    def write(self, data: bytes) -> int:
        if self._proc is None:
            raise RuntimeError("not spawned")
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        # pywinpty expects str, not bytes, for write(); decode safely
        try:
            self._proc.write(data.decode("utf-8", errors="replace"))
        except Exception as e:
            raise IOError(f"pty write failed: {e}") from e
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        if self._proc is None:
            return
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def tail(self, n_chars: Optional[int] = None) -> str:
        with self._lock:
            s = "".join(self._tail_buf)
        if n_chars is not None and len(s) > n_chars:
            return s[-n_chars:]
        return s

    # ------------------------- internals -------------------------

    def _append_tail(self, chunk: str) -> None:
        with self._lock:
            self._tail_buf.append(chunk)
            self._tail_len += len(chunk)
            while self._tail_len > self._tail_capacity and self._tail_buf:
                dropped = self._tail_buf.popleft()
                self._tail_len -= len(dropped)

    def _reader_loop(self) -> None:
        proc = self._proc
        assert proc is not None
        while not self._stop.is_set():
            if not proc.isalive():
                # drain remaining
                try:
                    leftover = proc.read(4096)
                    if leftover:
                        self._on_chunk(leftover)
                except Exception:
                    pass
                break
            try:
                data = proc.read(self.spec.cols * 4 or 4096)
            except EOFError:
                break
            except Exception:
                # transient; back off
                time.sleep(0.05)
                continue
            if not data:
                time.sleep(0.02)
                continue
            self._on_chunk(data)
        self._stop.set()

    def _on_chunk(self, chunk) -> None:
        # pywinpty may return str or bytes depending on backend
        if isinstance(chunk, bytes):
            try:
                s = chunk.decode("utf-8", errors="replace")
            except Exception:
                s = chunk.decode("latin-1", errors="replace")
        else:
            s = chunk
        self._append_tail(s)
        if self._on_data_cb is not None:
            try:
                self._on_data_cb(s)
            except Exception:
                pass


# ------------------------------- self-test -------------------------------

def _self_test_spawn_cmd() -> int:
    """Spawn cmd.exe, send `echo hello`, check tail contains 'hello'."""
    if not _HAS_WINPTY:
        print("pty_host.py self-test: SKIP (pywinpty not installed)")
        return 0

    captured: list[str] = []

    def _cb(chunk: str) -> None:
        captured.append(chunk)

    host = PtyHost(SpawnSpec(cmd="cmd.exe", cols=100, rows=30))
    host.set_on_data(_cb)
    host.spawn()
    try:
        time.sleep(1.2)  # let cmd.exe print its banner
        host.write(b"echo claude-q-smoke-token\r\n")
        # poll up to 4s for the token to appear
        deadline = time.monotonic() + 4.0
        tail = ""
        while time.monotonic() < deadline:
            tail = host.tail()
            if "claude-q-smoke-token" in tail:
                break
            time.sleep(0.15)
        assert "claude-q-smoke-token" in tail, \
            f"expected token in tail; got tail={tail[-500:]!r}"
        host.write(b"exit\r\n")
        host.wait(timeout=3.0)
    finally:
        host.terminate(force=True)

    print("pty_host.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test_spawn_cmd())
