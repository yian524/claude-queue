"""cli.py — entrypoint dispatcher for claude-q.

Subcommands
-----------
  start    run the wrapper: spawn claude under a PTY, relay keys,
           monitor idle, dispatch queue
  add      append a message to the active session's queue (call from
           another terminal; handy when Ctrl+Q isn't reachable)
  status   print the active session's status.json
  stop     terminate the active session (sends Ctrl+C then exits)
  list     list all pending entries in the active queue
  drop     drop a specific pending entry by id
  clear    drop all pending entries
  doctor   run sanity checks (pywinpty, claude binary, regex, PTY spawn)

Default / bare invocation
-------------------------
  `claude-q` with no subcommand runs `start --cmd claude`
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

# make local imports work when invoked via `python cli.py`
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import queue_store      # noqa: E402
import session          # noqa: E402
from config import load_config  # noqa: E402


# ------------------------- helpers -------------------------

def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _active_queue_path() -> Path:
    sid = session.require_active()
    return session.session_dir(sid) / "queue.jsonl"


def _active_status_path() -> Path:
    sid = session.require_active()
    return session.session_dir(sid) / "status.json"


def _active_session_path() -> Path:
    sid = session.require_active()
    return session.session_dir(sid) / "session.json"


# ------------------------- subcommand: start -------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Spawn claude under a PTY and run the wrapper's event loops."""
    import pty_host            # noqa: E402
    import terminal_relay      # noqa: E402
    from monitor import Monitor             # noqa: E402
    from status_bar import StatusBar, set_window_title  # noqa: E402

    cfg = load_config()

    # try to ensure UTF-8 in our own terminal (harmless if already set)
    try:
        os.system("chcp 65001 > nul")
    except Exception:
        pass

    # resolve target command
    cmd = args.cmd or "claude"
    if args.dry_run:
        cmd = "cmd.exe" if os.name == "nt" else "sh"
        print(f"[claude-q] --dry-run: target command is {cmd}")
    if not args.dry_run and shutil.which(cmd) is None:
        print(f"[claude-q] ERROR: {cmd!r} not found in PATH. Install it or use --cmd.",
              file=sys.stderr)
        return 2

    # allocate session
    sid = session.new_session_id()
    run_dir = session.session_dir(sid)
    session.set_active(sid)
    print(f"[claude-q] session {sid}")
    print(f"[claude-q] run dir: {run_dir}")
    print(f"[claude-q] queue:   {run_dir / 'queue.jsonl'}")
    print("[claude-q] toggle:  Ctrl+Q  (direct <-> queue)")
    print("[claude-q] starting claude... (type normally)")
    print("-" * 72)

    # resume prompt: existing pending entries from a previous abandoned session?
    # (active pointer was just overwritten; we don't auto-recover here, but
    # report any lingering pending from OTHER runs so the user sees them.)

    # spawn the child with dimensions matching the USER's actual terminal
    # (critical — Ink-based TUIs draw based on PTY size; wrong size = garbage)
    try:
        real_size = shutil.get_terminal_size(
            fallback=(cfg.pty_default_cols, cfg.pty_default_rows)
        )
        cols = max(40, real_size.columns)
        rows = max(10, real_size.lines)
    except Exception:
        cols, rows = cfg.pty_default_cols, cfg.pty_default_rows
    print(f"[claude-q] terminal size: {cols}x{rows}")

    spec = pty_host.SpawnSpec(cmd=cmd, cols=cols, rows=rows)
    host = pty_host.PtyHost(spec, tail_chars=cfg.tail_chars)

    # forward PTY bytes to our stdout (live passthrough)
    # CRITICAL: use sys.stdout.buffer (binary mode) to avoid Windows text-mode
    # translating \n to \r\n and corrupting Claude's Ink TUI redraws.
    #
    # Also: when the user is in queue mode we PAUSE stdout writes so Claude's
    # redraws don't stomp on the [queue]> prompt. Buffered bytes are flushed
    # when the user exits queue mode.
    stdout_buffer = getattr(sys.stdout, "buffer", None)

    import threading as _threading
    pause_lock = _threading.Lock()
    paused_flag = {"v": False}
    paused_buf: list[bytes] = []

    def _on_data(s: str) -> None:
        data = s.encode("utf-8", errors="replace")
        try:
            with pause_lock:
                if paused_flag["v"]:
                    paused_buf.append(data)
                    return
            if stdout_buffer is not None:
                stdout_buffer.write(data)
                stdout_buffer.flush()
            else:
                sys.stdout.write(s)
                sys.stdout.flush()
        except Exception:
            pass

    def _pause_output() -> None:
        with pause_lock:
            paused_flag["v"] = True

    def _resume_output() -> None:
        with pause_lock:
            paused_flag["v"] = False
            # flush everything Claude wrote while we were paused
            if paused_buf and stdout_buffer is not None:
                try:
                    stdout_buffer.write(b"".join(paused_buf))
                    stdout_buffer.flush()
                except Exception:
                    pass
                paused_buf.clear()

    host.set_on_data(_on_data)
    host.spawn()

    # persist session metadata
    st = session.SessionState(
        sid=sid,
        pid=os.getpid(),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        claude_cmd=cmd,
        cols=spec.cols,
        rows=spec.rows,
        dry_run=bool(args.dry_run),
    )
    session.write_session(st)

    # relay (keyboard -> PTY or queue). When the relay switches to queue
    # mode it owns the terminal via an ANSI alt-screen buffer, so we pause
    # Claude -> stdout writes. On exit we resume, and the terminal's native
    # alt-screen exit (\x1b[?1049l) restores Claude's view unchanged.
    def _on_mode_change(new_mode: str) -> None:
        if new_mode == "queue":
            _pause_output()
        else:
            _resume_output()

    relay = terminal_relay.TerminalRelay(
        queue_path=run_dir / "queue.jsonl",
        pty_write=lambda b: host.write(b),
        on_mode_change=_on_mode_change,
        session_id=sid,
    )
    relay.start()

    # monitor (idle -> dispatch)
    mon = Monitor(
        run_dir=run_dir,
        pty_tail_fn=host.tail,
        pty_write_fn=lambda b: host.write(b),
        get_mode=relay.get_mode,
        poll_interval_s=cfg.poll_interval_s,
        debounce_s=cfg.debounce_s,
        dispatch_commit_delay_s=cfg.dispatch_commit_delay_s,
        post_dispatch_backoff_s=cfg.post_dispatch_backoff_s,
        prompt_no_match_warn_s=cfg.prompt_no_match_warn_s,
    )
    mon.start()

    # status bar updater
    bar = StatusBar(
        run_dir=run_dir,
        provider=mon.snapshot,
        refresh_s=cfg.status_bar_refresh_s,
        enabled=cfg.status_bar_enabled,
    )
    bar.start()

    # main loop: wait until child exits OR stop sentinel appears
    exit_code = 0
    stop_sentinel = run_dir / "STOP"
    try:
        while host.is_alive():
            if stop_sentinel.exists():
                print("\n[claude-q] STOP sentinel detected; terminating.")
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        # forward to PTY; let claude handle interrupt
        try:
            host.write(b"\x03")
        except Exception:
            pass
    finally:
        relay.stop()
        mon.stop()
        bar.stop()
        try:
            rc = host.wait(timeout=2.0)
            if rc is not None:
                exit_code = rc
        except Exception:
            pass
        host.terminate(force=True)
        # don't clear ACTIVE so `claude-q status` after exit still works
        set_window_title("claude")
        print("\n[claude-q] session ended.")
    return exit_code


# ------------------------- subcommand: add -------------------------

def cmd_add(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("[claude-q] ERROR: empty message", file=sys.stderr)
        return 2
    qpath = _active_queue_path()
    eid = queue_store.push(qpath, text, source="claude-q-add")
    _print_json({"ok": True, "id": eid, "queue_len": queue_store.pending_len(qpath)})
    return 0


# ------------------------- subcommand: status -------------------------

def cmd_status(args: argparse.Namespace) -> int:
    sid = session.active_session()
    if not sid:
        _print_json({"active": None})
        return 1
    run_dir = session.session_dir(sid)
    status_path = run_dir / "status.json"
    data = {"active": sid, "run_dir": str(run_dir)}
    if status_path.exists():
        try:
            data["status"] = json.loads(status_path.read_text("utf-8"))
        except Exception as e:
            data["status_error"] = str(e)
    data["queue_len"] = queue_store.pending_len(run_dir / "queue.jsonl")
    _print_json(data)
    return 0


# ------------------------- subcommand: stop -------------------------

def cmd_stop(args: argparse.Namespace) -> int:
    """Drop a STOP sentinel file; the start loop polls for it and exits cleanly.

    Falls back to taskkill on Windows if the session main process is still
    running after a short grace period.
    """
    sid = session.active_session()
    if not sid:
        print("[claude-q] no active session")
        return 1
    st = session.read_session(sid)
    if st is None:
        print("[claude-q] ERROR: session.json missing", file=sys.stderr)
        return 2

    run_dir = session.session_dir(sid)
    sentinel = run_dir / "STOP"
    sentinel.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    print(f"[claude-q] STOP sentinel written for pid {st.pid}")

    # also try a best-effort hard kill in case the process is hung
    if os.name == "nt":
        try:
            import subprocess
            r = subprocess.run(
                ["taskkill", "/PID", str(st.pid), "/T", "/F"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                print("[claude-q] taskkill sent")
        except Exception:
            pass
    else:
        try:
            os.kill(st.pid, signal.SIGTERM)
        except ProcessLookupError:
            print("[claude-q] process already gone")
        except Exception as e:
            print(f"[claude-q] os.kill failed: {e}", file=sys.stderr)
    return 0


# ------------------------- subcommand: list -------------------------

def cmd_list(args: argparse.Namespace) -> int:
    qpath = _active_queue_path()
    entries = queue_store.list_all(qpath)
    pending = [e for e in entries if e.status == queue_store.STATUS_PENDING]
    if args.all:
        rows = entries
    else:
        rows = pending
    print(f"[claude-q] total={len(entries)} pending={len(pending)}")
    for e in rows:
        preview = e.text if len(e.text) <= 80 else e.text[:77] + "..."
        print(f"  [{e.status:<7}] {e.id}  {preview}")
    return 0


# ------------------------- subcommand: drop -------------------------

def cmd_drop(args: argparse.Namespace) -> int:
    qpath = _active_queue_path()
    ok = queue_store.drop(qpath, args.id)
    _print_json({"ok": ok, "id": args.id,
                 "queue_len": queue_store.pending_len(qpath)})
    return 0 if ok else 1


# ------------------------- subcommand: clear -------------------------

def cmd_clear(args: argparse.Namespace) -> int:
    qpath = _active_queue_path()
    n = queue_store.clear(qpath)
    _print_json({"dropped": n,
                 "queue_len": queue_store.pending_len(qpath)})
    return 0


# ------------------------- subcommand: doctor -------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    print("[claude-q] doctor")
    print("-" * 56)

    # 1. platform
    print(f"  platform:          {sys.platform}")
    print(f"  python:            {sys.version.splitlines()[0]}")

    # 2. pywinpty
    try:
        import winpty  # noqa: F401
        v = getattr(winpty, "__version__", "ok")
        print(f"  pywinpty:          OK ({v})")
    except Exception as e:
        print(f"  pywinpty:          FAIL ({e})")
        return 2

    # 3. prompt_toolkit (used elsewhere later; just confirm present)
    try:
        import prompt_toolkit  # noqa: F401
        print(f"  prompt_toolkit:    OK ({prompt_toolkit.__version__})")
    except Exception as e:
        print(f"  prompt_toolkit:    FAIL ({e})")

    # 4. claude binary
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"  claude:            OK ({claude_path})")
    else:
        print("  claude:            MISSING (not in PATH) "
              "— dry-run still works with --cmd cmd.exe")

    # 5. run dir writable
    try:
        rr = load_config().resolved_run_root()
        rr.mkdir(parents=True, exist_ok=True)
        probe = rr / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  run root writable: OK ({rr})")
    except Exception as e:
        print(f"  run root writable: FAIL ({e})")
        return 2

    # 6. PTY spawn smoke
    try:
        import pty_host
        spec = pty_host.SpawnSpec(cmd="cmd.exe", cols=80, rows=24)
        host = pty_host.PtyHost(spec)
        host.spawn()
        time.sleep(0.8)
        host.write(b"echo doctor-ok\r\n")
        deadline = time.monotonic() + 3.0
        tail = ""
        while time.monotonic() < deadline:
            tail = host.tail()
            if "doctor-ok" in tail:
                break
            time.sleep(0.15)
        host.write(b"exit\r\n")
        host.wait(timeout=2.0)
        host.terminate(force=True)
        if "doctor-ok" in tail:
            print("  pty spawn (cmd):   OK")
        else:
            print(f"  pty spawn (cmd):   FAIL (tail={tail[-200:]!r})")
            return 2
    except Exception as e:
        print(f"  pty spawn (cmd):   FAIL ({e})")
        return 2

    # 7. idle detector regex sanity
    try:
        import idle_detector
        fake = "│ >                                │"
        assert idle_detector.PROMPT_RE.search(fake)
        print("  prompt regex:      OK")
    except Exception as e:
        print(f"  prompt regex:      FAIL ({e})")
        return 2

    # 8. active session
    sid = session.active_session()
    print(f"  active session:    {sid or '(none)'}")

    print("-" * 56)
    print("[claude-q] doctor: all checks passed")
    return 0


# ------------------------- argparse plumbing -------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-q",
        description="Type-ahead FIFO queue wrapper for Claude Code CLI "
                    "(Windows, pywinpty-based)",
    )
    sub = p.add_subparsers(dest="cmd")

    p_start = sub.add_parser("start", help="spawn claude + queue wrapper")
    p_start.add_argument("--cmd", default=None,
                         help="command to wrap (default: claude)")
    p_start.add_argument("--dry-run", action="store_true",
                         help="wrap cmd.exe/sh instead of claude (for smoke testing)")
    p_start.set_defaults(func=cmd_start)

    p_add = sub.add_parser("add", help="enqueue a message into the active session")
    p_add.add_argument("text", nargs="+", help="message text (will be joined by spaces)")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("status", help="show active session status")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="signal the active session to stop")
    p_stop.set_defaults(func=cmd_stop)

    p_list = sub.add_parser("list", help="list queue entries")
    p_list.add_argument("--all", action="store_true",
                        help="include sent/dropped entries")
    p_list.set_defaults(func=cmd_list)

    p_drop = sub.add_parser("drop", help="drop a pending entry by id")
    p_drop.add_argument("id")
    p_drop.set_defaults(func=cmd_drop)

    p_clear = sub.add_parser("clear", help="drop all pending entries")
    p_clear.set_defaults(func=cmd_clear)

    p_doctor = sub.add_parser("doctor", help="run sanity checks")
    p_doctor.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # bare `claude-q` -> default to start with claude
    if not argv:
        argv = ["start"]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"[claude-q] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
