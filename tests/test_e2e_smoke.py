"""End-to-end smoke test for claude-q.

Covers the chain that DOESN'T need a real keyboard:
  - fresh session creation via session.set_active
  - `claude-q add` round-trip through cli.main
  - `claude-q list / drop / clear`
  - a short-running Monitor loop dispatches a queued entry into a fake
    PTY that reports idle output

The interactive key-relay is exercised by terminal_relay.py's self-test.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import cli                  # noqa: E402
import queue_store          # noqa: E402
import session              # noqa: E402
from config import load_config  # noqa: E402


@pytest.fixture
def isolated_run_root(tmp_path, monkeypatch):
    """Point the run root at a temp dir so this test never touches real state."""
    monkeypatch.setenv("HOME", str(tmp_path))                # posix
    monkeypatch.setenv("USERPROFILE", str(tmp_path))         # windows
    # cache-busting: reset the ACTIVE pointer if lying around
    monkeypatch.setattr(load_config, "__wrapped__", load_config, raising=False)
    yield tmp_path


def test_cli_add_list_drop_clear(isolated_run_root, capsys):
    # arrange: synthesise an active session
    sid = session.new_session_id()
    rd = session.session_dir(sid)
    session.set_active(sid)

    # add
    rc = cli.main(["add", "first message"])
    assert rc == 0
    out1 = json.loads(capsys.readouterr().out)
    assert out1["ok"] is True
    assert out1["queue_len"] == 1
    first_id = out1["id"]

    rc = cli.main(["add", "second", "message"])
    assert rc == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["queue_len"] == 2

    # list (only pending)
    rc = cli.main(["list"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "total=2 pending=2" in text
    assert "first message" in text
    assert "second message" in text

    # drop first
    rc = cli.main(["drop", first_id])
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert d["ok"] is True
    assert d["queue_len"] == 1

    # clear
    rc = cli.main(["clear"])
    assert rc == 0
    c = json.loads(capsys.readouterr().out)
    assert c["dropped"] == 1
    assert c["queue_len"] == 0


def test_monitor_dispatches_queued_entry(isolated_run_root):
    from monitor import Monitor

    sid = session.new_session_id()
    rd = session.session_dir(sid)
    session.set_active(sid)

    written = []
    mode = ["direct"]
    # fake an idle pane (empty prompt, no busy marker)
    fake_tail = [
        "some previous output\n"
        "╭──────────────────────────╮\n"
        "│ >                        │\n"
        "╰──────────────────────────╯\n"
    ]

    queue_store.push(rd / "queue.jsonl", "auto-dispatched-hello")

    mon = Monitor(
        run_dir=rd,
        pty_tail_fn=lambda: fake_tail[0],
        pty_write_fn=lambda b: (written.append(b) or len(b)),
        get_mode=lambda: mode[0],
        poll_interval_s=0.05,
        debounce_s=0.1,
        dispatch_commit_delay_s=0.02,
        post_dispatch_backoff_s=0.1,
        startup_grace_s=0.1,
    )
    mon.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if written:
            break
        time.sleep(0.05)
    mon.stop()
    # release FileHandler for Windows tempdir cleanup
    for h in list(mon._logger.handlers):
        h.close()
        mon._logger.removeHandler(h)

    assert len(written) == 1
    assert b"auto-dispatched-hello" in written[0]
    assert queue_store.pending_len(rd / "queue.jsonl") == 0


def test_status_with_no_active(monkeypatch, isolated_run_root, capsys):
    # explicitly clear ACTIVE
    session.clear_active()
    rc = cli.main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["active"] is None
