"""scheduler_tick.py - one-shot dispatch sweep, invoked by Windows Task
Scheduler every minute.

Responsibilities
----------------
1. Scan every session dir under ~/.claude/run/claude-q/*/
2. For each dir, check queue.jsonl for entries where:
     - status == pending
     - dispatch_at <= now
3. If the session appears to be alive (session.json.pid is running),
   the in-process monitor will dispatch — we do NOTHING and return.
4. If no session is alive, leave the pending entries untouched so they're
   picked up the moment the user next starts `claude -q`.

We intentionally DO NOT force-dispatch without a live session, because
our only delivery channel is a PTY attached to a running claude.exe. A
scheduled tick without an active session can only be a "reminder to
process these" rather than an actual dispatch.

Exit code is always 0 unless catastrophic I/O error. Windows Task
Scheduler logs stdout/stderr to the task history.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import queue_store  # noqa: E402
import session      # noqa: E402


def _process_is_alive(pid: int) -> bool:
    """Best-effort check whether a PID is still running on Windows."""
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def sweep() -> dict:
    """Scan all session dirs; report what we'd dispatch.

    Returns summary dict for logging.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    root = session.run_root()
    summary = {
        "ts": now_iso,
        "sessions_checked": 0,
        "alive_sessions": 0,
        "ready_entries": 0,
        "overdue_entries": 0,
    }
    if not root.exists():
        return summary

    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        summary["sessions_checked"] += 1

        # check alive
        sid = run_dir.name
        st = session.read_session(sid)
        alive = st is not None and _process_is_alive(st.pid)
        if alive:
            summary["alive_sessions"] += 1

        qpath = run_dir / "queue.jsonl"
        if not qpath.exists():
            continue

        ready = queue_store.dispatch_ready_len(qpath, now_iso=now_iso)
        summary["ready_entries"] += ready

        # count overdue (scheduled > 1min past but still pending)
        entries = queue_store.list_all(qpath)
        for e in entries:
            if (e.status == queue_store.STATUS_PENDING
                    and e.dispatch_at is not None
                    and e.dispatch_at < now_iso):
                summary["overdue_entries"] += 1

    return summary


def append_log(summary: dict) -> None:
    """Append the tick summary to a rolling log the user can inspect."""
    log_path = session.run_root() / "_scheduler_tick.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notify_if_overdue(summary: dict) -> None:
    """If there are overdue entries but no active session, try to notify.

    Uses Windows `msg` command (built-in on all Windows versions). This is
    best-effort; if it fails silently that's OK — the log still records it.
    """
    if summary["overdue_entries"] <= 0 or summary["alive_sessions"] > 0:
        return
    try:
        import subprocess
        text = (f"claude-q: {summary['overdue_entries']} scheduled "
                f"message(s) are ready. Run `claude -q` to process.")
        username = os.environ.get("USERNAME", "*")
        subprocess.run(
            ["msg", username, "/TIME:15", text],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def main() -> int:
    """Entry point for Windows Task Scheduler."""
    try:
        summary = sweep()
    except Exception as e:
        line = json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                           "error": f"{type(e).__name__}: {e}"})
        print(line)
        try:
            append_log({"error": line})
        except Exception:
            pass
        return 1
    append_log(summary)
    notify_if_overdue(summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
