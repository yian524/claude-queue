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


def test_v0_4_11_defaults_pinned():
    """Regression: ASAP latency is governed by three constants. Drift in
    any of them takes user-visible queue dispatch from ~5s back to
    20-75s. Pin all three so a future inadvertent edit fails CI."""
    import inspect
    from pty_host import PtyHost
    from monitor import Monitor

    pty_sig = inspect.signature(PtyHost.__init__)
    assert pty_sig.parameters["tail_chars"].default == 40000, (
        "PTY tail buffer must stay ≥ 40000 chars for 2.1.121 status-bar "
        "redraws not to push the prompt glyph out of view"
    )

    mon_sig = inspect.signature(Monitor.__init__)
    assert mon_sig.parameters["force_dispatch_after_stuck_s"].default == 5.0, (
        "L2 force-dispatch must default to 5s; longer values make ASAP "
        "feel laggy when PROMPT_RE can't match the current Claude TUI"
    )


def test_config_and_monitor_defaults_stay_in_sync():
    """Regression v0.4.16: `cli.py:231` constructs Monitor with values
    from `config.Config`, NOT from `Monitor.__init__` defaults. When the
    two drifted apart in v0.4.7-v0.4.15, every tuning the developer
    "shipped" by editing Monitor.__init__ silently bypassed production
    while pytest stayed green (tests construct Monitor directly).

    Pin BOTH sources of truth here. If a future contributor edits one
    without the other, this test fails loudly.
    """
    import inspect
    from monitor import Monitor
    from config import Config
    from pty_host import PtyHost

    cfg = Config()
    mon_sig = inspect.signature(Monitor.__init__)
    pty_sig = inspect.signature(PtyHost.__init__)

    pairs = [
        ("debounce_s", cfg.debounce_s, mon_sig.parameters["debounce_s"].default),
        ("poll_interval_s", cfg.poll_interval_s, mon_sig.parameters["poll_interval_s"].default),
        ("dispatch_commit_delay_s", cfg.dispatch_commit_delay_s,
         mon_sig.parameters["dispatch_commit_delay_s"].default),
        ("post_dispatch_backoff_s", cfg.post_dispatch_backoff_s,
         mon_sig.parameters["post_dispatch_backoff_s"].default),
        ("tail_chars", cfg.tail_chars, pty_sig.parameters["tail_chars"].default),
    ]
    drift = [(n, c, m) for n, c, m in pairs if c != m]
    assert not drift, (
        "config.py defaults DRIFTED from Monitor/PtyHost defaults — "
        "cli.py uses config.py at runtime so any drift means tuned "
        "values don't reach production. Drifted: " +
        ", ".join(f"{n}: config={c} ≠ code={m}" for n, c, m in drift)
    )


def test_actual_runtime_uses_fast_defaults():
    """Belt-and-suspenders: confirm the values cli.py would actually
    pass to Monitor are the fast post-v0.4.16 ones, not the v0.1 defaults.
    """
    from config import Config
    cfg = Config()
    assert cfg.debounce_s == 0.25, f"production debounce_s drifted to {cfg.debounce_s}"
    assert cfg.poll_interval_s == 0.1, f"production poll_interval drifted to {cfg.poll_interval_s}"
    assert cfg.post_dispatch_backoff_s == 0.5, (
        f"production post_dispatch_backoff drifted to {cfg.post_dispatch_backoff_s} — "
        f"if this is 3.0 again, queues will feel 'half-a-beat slow'"
    )
    assert cfg.dispatch_commit_delay_s == 0.03
    assert cfg.tail_chars == 40000


def test_native_claude_commands_pass_through_as_queued_text():
    """v0.4.12: when the user types `/clear`, `/init`, `/compact`, etc.
    in queue mode, the literal string must be queued as a plain entry —
    Claude's TUI handles the command on dispatch. Previously these got
    rejected with `unknown command`."""
    import slash_commands as sc

    for native in (
        "/clear", "/init", "/compact", "/help", "/cost", "/usage",
        "/model", "/config", "/permissions", "/agents", "/skills",
        "/mcp", "/memory", "/plan", "/release-notes",
    ):
        r = sc.parse(native)
        assert isinstance(r, sc.QueueRequest), (
            f"{native} must be QueueRequest, got {type(r).__name__}"
        )
        assert r.text == native
        assert r.dispatch_at is None
        assert r.priority == 0


def test_unknown_slash_commands_also_pass_through():
    """Future-proofing: any unknown /command (e.g. ones Claude Code
    ships in a later version that we haven't catalogued) must also
    queue as plain text instead of erroring."""
    import slash_commands as sc

    r = sc.parse("/some-future-command --flag value")
    assert isinstance(r, sc.QueueRequest)
    assert r.text == "/some-future-command --flag value"


def test_claudeenter_returns_request_type():
    """`/claudeenter` triggers the relay's native-command dropdown UI;
    parsing must return ClaudeEnterRequest (not QueueRequest)."""
    import slash_commands as sc

    r = sc.parse("/claudeenter")
    assert isinstance(r, sc.ClaudeEnterRequest)


def test_filter_commands_split_namespace_v0_4_17():
    """v0.4.17 split-namespace dropdown:
       - single `/` → only queue-internal commands
       - `//` (claude_picker=True) → only native Claude commands
       Mixing the two pools (the v0.4.12 behaviour) made the dropdown
       too long and confusing; users wanted them separated to match
       Claude's own native picker UX.
    """
    import slash_commands as sc

    queue_only = sc.filter_commands("")
    qnames = {c["name"] for c in queue_only}
    # Must include queue-internal commands
    assert {"/wait", "/at", "/priority", "/now", "/drop", "/qclear",
            "/qhelp", "/claudeenter"}.issubset(qnames)
    # Must NOT pollute with native commands
    assert "/clear" not in qnames
    assert "/init" not in qnames
    # Every result is kind=queue
    assert all(c.get("kind") == "queue" for c in queue_only)

    native_only = sc.filter_commands("", claude_picker=True)
    nnames = {c["name"] for c in native_only}
    # Must include the built-in 22 (and possibly more from skills/plugins)
    assert {"/clear", "/init", "/help", "/compact", "/model"}.issubset(nnames)
    # Must NOT pollute with queue-internal commands
    assert "/wait" not in nnames
    assert "/qclear" not in nnames
    # Every result is kind=claude
    assert all(c.get("kind") == "claude" for c in native_only)


def test_double_slash_normalizes_to_single_slash():
    """v0.4.17: typing `//foo` in queue mode is the user's signal to
    queue Claude's native /foo command. The parser must strip exactly
    one leading slash so the dispatched text is `/foo`, not `//foo`."""
    import slash_commands as sc

    r = sc.parse("//clear")
    assert isinstance(r, sc.QueueRequest) and r.text == "/clear"

    r = sc.parse("//expert-roundtable why is the sky blue?")
    assert isinstance(r, sc.QueueRequest)
    assert r.text == "/expert-roundtable why is the sky blue?"

    r = sc.parse("//scheduler:schedule-add daily 9am")
    assert isinstance(r, sc.QueueRequest)
    assert r.text == "/scheduler:schedule-add daily 9am"


def test_discover_native_commands_picks_up_project_level_commands(tmp_path):
    """v0.4.18: a `<project>/.claude/commands/*.md` file must surface
    in the `//` dropdown when the queue is running inside that project.
    Claude's own `/` picker does this; we must match it."""
    import slash_commands as sc

    # Create a project-level command file under tmp_path
    cmd_dir = tmp_path / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "codex.md").write_text(
        "---\ndescription: Run codex review\n---\n\nRun codex on the current branch.\n",
        encoding="utf-8",
    )
    (cmd_dir / "expert.md").write_text(
        "Convene panel of experts on the question below.\n",
        encoding="utf-8",
    )

    sc._reset_native_cache()
    discovered = sc.discover_native_commands(cwd=tmp_path)
    by_src = {}
    for c in discovered:
        by_src.setdefault(c["source"], []).append(c["name"])

    assert "/codex" in by_src.get("project-cmd", []), (
        f"project-cmd /codex not found; got sources={list(by_src.keys())}"
    )
    assert "/expert" in by_src.get("project-cmd", []), (
        f"project-cmd /expert not found; got {by_src.get('project-cmd')}"
    )
    # The summary for /codex should come from frontmatter description
    codex_entry = next(c for c in discovered if c["name"] == "/codex")
    assert "Run codex" in codex_entry["summary"]


def test_discover_native_commands_picks_up_user_level_commands(tmp_path, monkeypatch):
    """v0.4.18: ~/.claude/commands/*.md (user-scope custom commands)
    must also surface — Claude's native picker shows them across all
    sessions."""
    import slash_commands as sc
    from pathlib import Path

    # monkeypatch Path.home() to point at tmp
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    user_cmd_dir = fake_home / ".claude" / "commands"
    user_cmd_dir.mkdir(parents=True)
    (user_cmd_dir / "insights.md").write_text(
        "---\ndescription: Generate a report analyzing your Claude Code sessions\n---\n",
        encoding="utf-8",
    )

    sc._reset_native_cache()
    discovered = sc.discover_native_commands(cwd=tmp_path)  # tmp_path has no project commands
    user_cmds = [c for c in discovered if c.get("source") == "user-cmd"]
    user_names = {c["name"] for c in user_cmds}

    assert "/insights" in user_names, (
        f"user-cmd /insights not found; got user-cmds={user_names}"
    )
    insights = next(c for c in user_cmds if c["name"] == "/insights")
    assert "report" in insights["summary"].lower()


def test_discover_native_commands_includes_skills_and_plugins():
    """v0.4.17: discover_native_commands() must scan ~/.claude/skills/
    and ~/.claude/plugins/ for installed commands, not just hardcoded
    built-ins. Result depends on the host machine, so we only assert
    presence of built-in 22 + that source taxonomy is correctly tagged.
    """
    import slash_commands as sc

    sc._reset_native_cache()
    discovered = sc.discover_native_commands()

    # Built-in 22 must always be there
    builtin = [c for c in discovered if c.get("source") == "builtin"]
    assert len(builtin) == 22, f"expected 22 built-ins, got {len(builtin)}"
    builtin_names = {c["name"] for c in builtin}
    assert {"/clear", "/init", "/help", "/compact"}.issubset(builtin_names)

    # All entries are kind=claude
    assert all(c.get("kind") == "claude" for c in discovered)
    # All entries have a recognized source tag
    valid_sources = {"builtin", "skill", "plugin", "user-cmd", "project-cmd"}
    assert all(c.get("source") in valid_sources for c in discovered), (
        f"unknown source tag in {[c.get('source') for c in discovered]}"
    )
    # All names start with /
    assert all(c["name"].startswith("/") for c in discovered)


def test_native_commands_helper():
    """`native_commands()` returns the curated native Claude list,
    used by /claudeenter to populate the dropdown."""
    import slash_commands as sc

    nc = sc.native_commands()
    names = {c["name"] for c in nc}
    assert "/clear" in names
    assert "/init" in names
    # Every entry must be tagged kind=claude
    assert all(c.get("kind") == "claude" for c in nc)


def test_qclear_and_qhelp_renamed_correctly():
    """Queue-internal /clear and /help were renamed /qclear and /qhelp
    so Claude's own /clear and /help can pass through. Pin both halves."""
    import slash_commands as sc

    # /qclear → ClearRequest
    assert isinstance(sc.parse("/qclear"), sc.ClearRequest)
    # /qhelp → HelpRequest
    assert isinstance(sc.parse("/qhelp"), sc.HelpRequest)

    # /clear → QueueRequest (passes to Claude)
    r = sc.parse("/clear")
    assert isinstance(r, sc.QueueRequest) and r.text == "/clear"
    # /help → QueueRequest (passes to Claude)
    r = sc.parse("/help")
    assert isinstance(r, sc.QueueRequest) and r.text == "/help"


def test_l1_prompt_scan_window_finds_prompt_buried_under_status_fragments():
    """Regression v0.4.11: PROMPT_RE must scan a wide-enough window of
    tail lines to find the prompt glyph even when claude-code 2.1.121's
    status bar churns 15+ single-character fragments at the bottom of
    tail. v0.4.10 used 10 lines and never caught the prompt; v0.4.11
    uses 30 and catches it within ~0.5s instead of waiting for L2."""
    import idle_detector

    # Prompt at line -22, then 20 status-bar fragments at the bottom
    tail = (
        "previous answer text\n"
        "more answer text\n"
        "╭─────╮\n"
        "│ >   │\n"
        "╰─────╯\n"
        + "\n".join(str(i) for i in range(20)) + "\n"
    )
    s = idle_detector.IdleState()
    r = idle_detector.is_idle(tail, s, now=1.0)
    assert r.reasons["prompt_visible"] is True, (
        "L1 must catch a prompt buried up to line -22 under "
        "status-bar fragments (this is the typical 2.1.121 layout)"
    )


def test_l1_busy_regex_catches_2_1_121_formats():
    """Regression: idle_detector must mark 2.1.121's spinner formats as
    busy (no-space spinner, plain ellipsis, spinnerless 'thinking with')."""
    import idle_detector

    captures = [
        ("✻Manifesting…\n", "no-space spinner"),
        ("Manifesting…\n", "verb alone with ellipsis"),
        ("almost done thinking with high effort\n", "spinnerless thinking"),
        ("·Manifesting… 5\n", "no-space spinner with token count"),
    ]
    for tail, name in captures:
        s = idle_detector.IdleState()
        r = idle_detector.is_idle(tail, s, now=1.0, debounce_s=0.6)
        assert r.reasons["not_busy"] is False, (
            f"L1 regression: {name!r} should mark busy; got {r.reasons}"
        )


def test_dispatch_fires_while_user_is_in_queue_mode():
    """Regression v0.4.9: dispatch must NOT be gated on direct mode.
    Previously, a user sitting in the Ctrl+Q queue input pane would
    block dispatch (the dispatcher returned early on `mode != 'direct'`).
    That defeated the whole point of a queue — enqueueing an item is
    an explicit "fire when idle" signal, so we dispatch regardless of
    whichever pane the user happens to be composing in."""
    import tempfile
    from pathlib import Path
    from monitor import Monitor
    from queue_store import push as q_push

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        rd = Path(td)
        written = []
        idle_tail = (
            "previous answer text\n"
            "╭──────────────────────────╮\n"
            "│ >                        │\n"
            "╰──────────────────────────╯\n"
        )
        # User is sitting in queue mode (Ctrl+Q pane open) the whole time.
        mode = ["queue"]
        mon = Monitor(
            run_dir=rd,
            pty_tail_fn=lambda: idle_tail,
            pty_write_fn=lambda b: (written.append(b) or len(b)),
            get_mode=lambda: mode[0],
            poll_interval_s=0.05,
            debounce_s=0.1,
            dispatch_commit_delay_s=0.02,
            post_dispatch_backoff_s=0.1,
            startup_grace_s=0.1,
        )
        q_push(rd / "queue.jsonl", "queued-while-in-queue-mode")
        mon.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not written:
            time.sleep(0.05)
        mon.stop()
        for h in list(mon._logger.handlers):
            h.close()
            mon._logger.removeHandler(h)

        assert len(written) == 1, (
            "Dispatch must fire while user is in queue mode "
            "(matches 'fire and forget' user expectation)"
        )
        assert b"queued-while-in-queue-mode" in written[0]


def test_l2_force_dispatch_only_when_stable():
    """Regression: L2 must NOT force-dispatch into a churning screen.
    Stability is the load-bearing safety guarantee."""
    import tempfile
    from pathlib import Path
    from monitor import Monitor
    from queue_store import push as q_push

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        rd = Path(td)
        written = []
        counter = [0]

        def churning_tail():
            counter[0] += 1
            return f"output frame {counter[0]}\nno prompt visible\n"

        mon = Monitor(
            run_dir=rd,
            pty_tail_fn=churning_tail,
            pty_write_fn=lambda b: (written.append(b) or len(b)),
            get_mode=lambda: "direct",
            poll_interval_s=0.02,
            debounce_s=0.05,
            dispatch_commit_delay_s=0.01,
            post_dispatch_backoff_s=0.05,
            startup_grace_s=0.05,
            force_dispatch_after_stuck_s=0.3,  # aggressive for fast test
        )
        q_push(rd / "queue.jsonl", "must NOT dispatch")
        mon.start()
        time.sleep(1.5)  # well past the 0.3s threshold
        mon.stop()
        for h in list(mon._logger.handlers):
            h.close()
            mon._logger.removeHandler(h)

        assert len(written) == 0, (
            "L2 must not force-dispatch into a churning screen "
            "(would interrupt active generation)"
        )
