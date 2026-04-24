# claude-q — Type-Ahead FIFO Queue Wrapper for Claude Code CLI

Wrap Claude Code in a pseudo-terminal so you can **type the next question
while Claude is still answering the previous one**. The wrapper catches
your new input into a FIFO queue and auto-sends it the moment Claude
returns to an empty prompt.

- **zero tmux dependency** — pure Python + `pywinpty`
- **one dedicated venv** at `~/.claude/scripts/claude-queue/.venv` (does not
  touch your thesis venv or global Python)
- **transparent passthrough** — 95 % of the time you use it, it feels
  identical to plain `claude`; the difference is when `Ctrl+Q` is pressed

---

## Install summary

Already done on this machine. For a fresh install:

```bat
cd %USERPROFILE%\.claude\scripts\claude-queue
python -m venv .venv
.venv\Scripts\python.exe -m pip install pywinpty prompt_toolkit pytest
```

Add `%USERPROFILE%\.claude\bin` to your PATH if you want to call `claude-q`
directly from any shell.

---

## Quick start

```bat
claude-q doctor            # confirm all green
claude-q                   # launches Claude Code under the wrapper
```

Inside the wrapper:

- Type normally → same as native `claude`
- Press **Ctrl+Q** → the wrapper switches to **queue mode**; your next line
  of typing is echoed locally (not sent to Claude). Press **Enter** to push
  it into the FIFO. You return to direct mode automatically.
- Press **Esc** while in queue mode to cancel the buffered input.
- Press **Ctrl+C** → always forwarded to Claude so it can interrupt itself
  (if you were in queue mode, it also cancels the buffered input first).
- Window title shows live status: `claude-q │ queue:2 │ mode:direct │ idle`

When Claude finishes a turn and the prompt becomes empty, the background
monitor pops the head of the queue and sends it as if you had typed it.

---

## Enqueue from a second terminal

```bat
claude-q-add "write tests for foo.py"
claude-q list
claude-q drop 20260424T061254_xxxxxx
claude-q clear
```

Or via `claude-q add ...` directly.

---

## Subcommands

| Command | Purpose |
| --- | --- |
| `claude-q start` (or bare `claude-q`) | Start the wrapped session |
| `claude-q add <text>` | Enqueue a message into the active session |
| `claude-q status` | Print the active session's JSON status |
| `claude-q list [--all]` | List pending (or all) queue entries |
| `claude-q drop <id>` | Drop a single pending entry |
| `claude-q clear` | Drop every pending entry |
| `claude-q stop` | Signal the active session to stop |
| `claude-q doctor` | Sanity checks (pywinpty, claude, PTY spawn, regex) |

`claude-q start --dry-run` replaces `claude` with `cmd.exe` for smoke tests.

---

## How idle detection works

`idle_detector.py` uses a composite AND of three signals:

1. **Empty prompt visible** — regex `│\s*>\s*(│\s*)?$` matches in the last
   five non-empty lines of stripped output (i.e. Claude's prompt is drawn
   and the input area is empty).
2. **No busy marker** — none of `Tokens:`, `esc to interrupt`, `✢ ✻ ✽ ✺`,
   `Thinking`, `Computing`, `Working` appears in the last 10 lines.
3. **Content stable** — the hash of the stripped pane tail has not changed
   for ≥ `debounce_s` (default 600 ms).

If signal 1 has not matched for 30 s but signals 2–3 still hold, the status
bar shows `⚠drift` so you know the regex may need updating (e.g. if Claude
changes its prompt glyph).

---

## File layout

```
~/.claude/scripts/claude-queue/
├── .venv/                  # dedicated virtualenv (isolated from thesis)
├── cli.py                  # argparse entrypoint
├── session.py              # session id + paths + ACTIVE pointer
├── config.py               # defaults + optional ~/.claude/claude-q.toml
├── queue_store.py          # atomic JSONL FIFO
├── pty_host.py             # pywinpty wrapper
├── idle_detector.py        # three-signal AND + debounce
├── monitor.py              # background dispatcher thread
├── terminal_relay.py       # keyboard loop + mode toggle
├── status_bar.py           # window-title status + status.json
├── README.md               # this file
└── tests/                  # pytest suite (to be expanded)

~/.claude/bin/
├── claude-q.cmd            # main wrapper
└── claude-q-add.cmd        # enqueue-from-elsewhere shortcut

~/.claude/run/claude-q/<session_id>/
├── session.json            # pid, started_at, claude_cmd, dimensions
├── queue.jsonl             # append-only FIFO (crash-safe, inspectable)
├── status.json             # live monitor snapshot (polled by status_bar)
└── monitor.log             # debug log
```

---

## Current limitations

* **Windows only** today. POSIX support via `ptyprocess` is a straight
  drop-in for `pty_host.py` but not wired up yet.
* Some special-key sequences beyond arrows / Home/End / F1-F4 may not be
  forwarded in direct mode; raw `claude` remains the fallback for
  keyboard-heavy interactions.
* If Claude Code changes its prompt glyph the idle regex will need an
  update (the wrapper will warn via `⚠drift` when that happens).

---

## Troubleshooting

* `claude: not found` → `claude-q doctor` will tell you. Either install
  Claude Code or pass `--cmd <path-to-claude>`.
* Garbled Chinese in the wrapper's own messages → run `chcp 65001` once in
  the terminal; the wrapper also sets the codepage on start.
* Queue entry never sent → check `monitor.log` in the run dir; most likely
  the idle detector never saw an empty prompt (prompt drift). Update
  `PROMPT_RE` in `idle_detector.py` to match the current prompt.

---

## Design rationale (short)

Native Claude Code has an unreliable behaviour when you hit Enter during
streaming (tracked in anthropics/claude-code issues #50246, #34835,
#33323, #26388, #1126). Existing third-party tools are all either batch
rate-limit queues (`JCSnap/claude-code-queue`, `vasiliyk/claude-queue`)
that ask you to run Claude unattended, or they require a second terminal
(`snomiao/agent-yes`). None fit the *inline type-ahead during an active
session* pattern that VS Code Copilot Chat provides. `claude-q` is a
minimal wrapper that solves exactly that gap while leaving the Claude TUI
untouched.
