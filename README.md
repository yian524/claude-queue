# claude-q

> Type-ahead FIFO queue wrapper for Claude Code CLI on Windows.
> Press `Ctrl+Q` mid-response to queue a follow-up question without
> interrupting Claude's current work — it's auto-dispatched when Claude
> returns to idle.

![license](https://img.shields.io/badge/license-MIT-blue)
![platform](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![status](https://img.shields.io/badge/status-beta-orange)

---

## The problem

You're chatting with `claude` in the terminal. Claude is writing a long
answer and you suddenly remember a follow-up question. In native Claude
Code:

* Pressing Enter while Claude is streaming **may interrupt** the current
  answer (behaviour varies between versions — see
  [anthropics/claude-code#50246](https://github.com/anthropics/claude-code/issues/50246),
  [#34835](https://github.com/anthropics/claude-code/issues/34835),
  [#33323](https://github.com/anthropics/claude-code/issues/33323)).
* Claude Code v2.1.x does ship a native queue, but its UI still mixes the
  queued text into the active response area, causing visual confusion and
  occasional loss.

`claude-q` solves this by wrapping Claude in a PTY and exposing a second,
completely isolated queue channel. Pressing `Ctrl+Q` pops up a dedicated
full-screen queue UI (via the terminal's alt-screen buffer — like vim or
less); you type the follow-up, hit Enter, and the UI disappears restoring
Claude's view exactly as it was. The queued message is auto-sent the
moment Claude's prompt becomes empty again.

---

## Quick start

### Requirements

* Windows 10 1809+ (for ConPTY)
* Python 3.10+
* [Claude Code CLI](https://github.com/anthropics/claude-code) installed
  and on your `PATH`

### Install

Clone into `~/.claude/scripts/` (recommended) or anywhere you like:

```powershell
git clone https://github.com/yian524/claude-queue.git "$env:USERPROFILE\.claude\scripts\claude-queue"
cd "$env:USERPROFILE\.claude\scripts\claude-queue"
python -m venv .venv
.venv\Scripts\python.exe -m pip install pywinpty prompt_toolkit pytest
```

Make the `claude -q ...` syntax work in your PowerShell sessions by
adding a function to your PowerShell `$PROFILE`:

```powershell
# Create PROFILE file if it doesn't exist yet
New-Item -Path $PROFILE -ItemType File -Force

# Append the claude wrapper function (this repo ships a ready-made one)
Get-Content "$env:USERPROFILE\.claude\scripts\claude-queue\bin\claude-profile.ps1" |
  Add-Content -Path $PROFILE
```

This works because **PowerShell functions take priority over anything
on `$PATH`**, so the function unconditionally catches every `claude ...`
invocation and decides whether to route to the wrapper (`-q`) or to the
real `claude.exe` (anything else).

For `cmd.exe` users or if you prefer a directory-based shim, the repo
also ships `bin/claude.cmd` and `bin/claude-q.cmd`. Place them in a
directory that appears on your `PATH` **before** the real
`claude.exe`'s directory. (On PowerShell the function profile route is
more reliable because PATHEXT preference quirks can cause `.exe` to
win over `.cmd` even when the `.cmd`'s directory is listed first.)

### Verify

```powershell
claude -q doctor
```

All eight checks should report `OK`.

### Run

```powershell
claude -q
```

Claude Code launches as usual. The difference: press `Ctrl+Q` mid-response
to drop into the queue UI.

> **Tip:** every other `claude <subcommand>` still works unchanged — the
> shim only intercepts when the first argument is `-q`. So `claude
> --version`, `claude --resume ...`, `/mcp`, etc. all behave normally.

> **Backward-compat:** the older `claude-q` / `claude-q-add` commands
> still exist as aliases if you already had scripts or muscle memory
> using them.

---

## Keyboard cheatsheet

| In direct mode ( `❯` prompt) | Effect |
|---|---|
| Any key | Forwarded to Claude unchanged |
| `Ctrl+Q` | **Switch to queue mode** (opens alt-screen UI) |
| `Ctrl+C` | Forwarded — let Claude interrupt itself |

| In queue mode (alt-screen UI) | Effect |
|---|---|
| Printable keys / IME | Appear in the `> ` input line |
| `Backspace` | Delete last char |
| `Enter` | **Queue the message** and return to Claude |
| `Esc` or `Ctrl+Q` | Cancel without queuing |

---

## Commands

| Command | Purpose |
|---|---|
| `claude -q` | Start the wrapped session |
| `claude-q start --cmd <path>` | Wrap a different executable (default: `claude`) |
| `claude-q start --dry-run` | Wrap `cmd.exe` for smoke-testing |
| `claude -q add "<text>"` | Append to the active queue from another terminal |
| `claude-q list [--all]` | Show pending (or all) queue entries |
| `claude-q drop <id>` | Drop one pending entry |
| `claude-q clear` | Drop every pending entry |
| `claude-q status` | JSON snapshot (queue length, idle state, mode) |
| `claude-q stop` | Terminate the active session |
| `claude-q doctor` | Environment sanity check |

---

## How it works

```
                   ┌──────────────────┐
                   │ Windows Terminal │
                   └────────┬─────────┘
                            │ stdin (ReadConsoleInputW)
                            ▼
   ┌──────────────────────────────────────────────────┐
   │  claude-q (Python)                                │
   │                                                   │
   │   ┌──────────────┐      ┌──────────────────────┐ │
   │   │ TerminalRelay│◄────▶│ queue_store (JSONL)  │ │
   │   └──────┬───────┘      └──────────────────────┘ │
   │          │ bytes                                   │
   │          ▼                                         │
   │   ┌──────────────┐      ┌──────────────────────┐ │
   │   │   pty_host   │◄────▶│ Monitor (idle watch) │ │
   │   └──────┬───────┘      └──────────────────────┘ │
   └──────────┼───────────────────────────────────────┘
              │ pywinpty (ConPTY)
              ▼
   ┌──────────────────┐
   │ claude.exe (Ink) │
   └──────────────────┘
```

* **`win_console_input.py`** — `ReadConsoleInputW` direct read so IME
  composition and Windows Terminal's ANSI reply injection don't pollute
  our input stream.
* **`pty_host.py`** — `pywinpty` wrapper; reader thread writes Claude's
  bytes to `sys.stdout.buffer` *and* a bounded tail buffer used by the
  idle detector.
* **`idle_detector.py`** — three-signal AND: empty prompt + no busy
  marker + content stable for `debounce_s` (default 600 ms). Supports
  both the v1 boxed prompt (`│ > │`) and v2 angle prompts (`❯`, `›`).
* **`monitor.py`** — background thread that, when the detector returns
  idle and the queue is non-empty, pops the head entry and writes it to
  the PTY.
* **`terminal_relay.py`** — keyboard loop. In direct mode every key is
  forwarded to the PTY. On `Ctrl+Q` we emit `\x1b[?1049h` to enter the
  terminal's alt-screen buffer, draw a dedicated queue UI there, then on
  exit emit `\x1b[?1049l` and the terminal restores Claude's main-screen
  view exactly as it was — no redraw fight with Ink.

---

## File layout

```
claude-queue/
├── LICENSE
├── README.md
├── __init__.py
├── cli.py                 # entrypoint (argparse dispatcher)
├── session.py             # session id + paths + ACTIVE pointer
├── config.py              # defaults + optional TOML override
├── queue_store.py         # atomic JSONL FIFO
├── pty_host.py            # pywinpty wrapper
├── win_console_input.py   # ReadConsoleInputW binding
├── idle_detector.py       # three-signal AND idle detection
├── monitor.py             # background dispatcher thread
├── terminal_relay.py      # keyboard <-> PTY bridge + queue UI
├── status_bar.py          # window-title status reporter
├── diag_keys.py           # standalone key-read diagnostic
└── tests/
    └── test_e2e_smoke.py  # pytest end-to-end suite
```

Runtime data lives under `~/.claude/run/claude-q/<session_id>/` and is
not versioned.

---

## Known limitations

* Windows only. Linux/macOS support is a future project — the alt-screen
  UI layer is portable, but `win_console_input.py` needs a `termios`
  equivalent.
* Some special-key combinations beyond arrows / Home/End / F1-F4 may not
  be forwarded in direct mode; fall back to plain `claude` for
  keyboard-heavy interactions like `/` slash-menu navigation if you hit
  trouble.
* Claude Code's own native type-ahead queue still exists side-by-side
  with ours — you can use either, but don't expect them to cooperate.
  Our queue is entirely isolated and runs the moment Claude's prompt is
  empty.

---

## Development

Run the self-tests and pytest suite:

```powershell
cd ~\.claude\scripts\claude-queue
.venv\Scripts\python.exe queue_store.py
.venv\Scripts\python.exe idle_detector.py
.venv\Scripts\python.exe terminal_relay.py
.venv\Scripts\python.exe -m pytest tests/
```

Keyboard diagnostic (prints hex codes as you press keys):

```powershell
.venv\Scripts\python.exe diag_keys.py
```

### Design notes

This project deliberately avoids:

* A separate window (tmux/screen) — users asked for a pure single-terminal
  experience.
* An embedded Python TUI library (Textual, prompt_toolkit full screen) for
  the Claude side — re-rendering Ink output through another layer is a
  losing game; we just relay bytes.
* A daemon / background service — everything runs inside the single
  `python cli.py` process for easy cleanup (`Ctrl+C` ends the whole thing).

---

## Contributing

PRs welcome. Please:

1. Open an issue first for non-trivial changes.
2. Run `pytest` and keep the self-tests green.
3. Follow the existing style (PEP 8, typed signatures where helpful,
   short docstrings).
4. Keep the dependency footprint small — `pywinpty` +
   `prompt_toolkit` + stdlib is the current budget.

---

## License

MIT — see [`LICENSE`](./LICENSE).

---

## Credits

* Built on top of [pywinpty](https://github.com/andfoy/pywinpty) by
  Andrés Felipe Zapata Mesa et al.
* Inspired by discussions in
  [anthropics/claude-code#50246](https://github.com/anthropics/claude-code/issues/50246).
* Authored by Sung with pair-programming assistance from Claude. For
  contact, please open an [issue](../../issues) on this repository.
