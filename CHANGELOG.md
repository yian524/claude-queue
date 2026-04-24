# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.3.4] - 2026-04-24

### Fixed
- **"Two input boxes" ghost on alt-screen exit.** Root cause: during
  queue mode, Claude's Ink TUI continued emitting full-screen redraw
  frames (status bar, spinner) which we buffered. On resume, replaying
  every buffered frame left residue from earlier frames that later ones
  didn't fully overwrite. Fixed by:
  - Clearing the visible screen (`\x1b[H\x1b[2J`) before the replay so
    the final frame draws on a clean slate.
  - Truncating the replay buffer to the last 16 KB — a single Ink frame
    is typically < 8 KB, so we reliably keep at least one complete
    frame and drop older ones that would have been overdrawn anyway.

## [0.3.3] - 2026-04-24

### Fixed
- **Intermittent "scheduled /wait never fires" bug.** Root cause: on long
  Claude answers + repeated TUI redraws (`auto mode` bar, `/mcp` status,
  spinner frames), the raw-bytes tail buffer (4 KB) filled up and the
  `❯` prompt line scrolled off before `idle_detector` saw it. Monitor
  logged `reasons.prompt_visible: False drift: True` for 45+ seconds
  while Claude was actually idle.
  - Increased `tail_chars` 4 KB → 16 KB so the prompt stays in view.
  - `PROMPT_RE` split into `PROMPT_RE_END` (line ending with prompt)
    and `PROMPT_RE_LINE` (whole line is prompt + whitespace) — more
    tolerant of modern Claude UI where `❯` sits on its own line.
  - Prompt-visibility search widened from last 5 non-empty lines to
    last 10.

### Added
- When monitor has held dispatch for 10s+ with drift detected, it dumps
  the last 5 stripped tail lines to `monitor.log` for diagnosis.

## [0.3.2] - 2026-04-24

### Added
- Queue-mode Pending list now shows scheduling info next to each entry:
  `★` for priority, `in Xs` for time-to-dispatch, `ASAP` for
  unscheduled. Entries are also rendered in the exact order the monitor
  will dispatch them (priority desc, dispatch_at asc, ts asc) so the UI
  matches reality.
- New slash commands for in-queue editing:
  - `/drop <N>` — drop pending entry #N (same numbering as the
    Pending list)
  - `/clear` — drop all pending entries

### Changed
- Monitor now logs ready-set transitions and dispatch blocks to
  `monitor.log`. When entries are ready but idle-detector is holding
  back, it logs the reasons (throttled to once every 3 seconds) so
  "why didn't my /wait fire?" reports are diagnosable.

## [0.3.1] - 2026-04-24

### Changed
- Dropped `/cancel` from the slash-command set. Esc and Ctrl+Q already
  cancel queue-mode input, so the extra verb was clutter. Typing
  `/cancel` now shows "unknown command".

### Fixed
- Queue-mode UI: cursor occasionally landed outside the input box after
  rapid keypresses. Each rendered line now prefixes `\x1b[K` (clear
  line) as a defensive measure, and `\x1b[H\x1b[J` replaces `\x1b[2J`
  for a slightly more conservative clear. Reduces rendering glitches
  on Windows Terminal under fast input.

## [0.3.0] - 2026-04-24

### Added
- **Scheduling: `/wait <duration>` and `/at <time>`.** Queue a message
  to be dispatched only after a delay (`/wait 5m`, `/wait 1h30m`,
  `/wait 90s`) or at an absolute time (`/at 14:30`,
  `/at 2026-04-25 14:30`). Monitor honours scheduling and
  priority.
- **Slash commands with autocomplete.** When you type `/` in the queue
  UI, a dropdown shows matching commands. `↑↓` navigate, `Tab`/`Enter`
  picks the template, `Esc` closes. Commands:
  - `/wait <dur> <msg>` — delayed dispatch
  - `/at <time> <msg>` — absolute-time dispatch
  - `/priority <msg>` — jump ahead of normal queue entries
  - `/now <msg>` — bypass idle check, dispatch immediately (WARNING)
  - `/cancel` — discard input, back to direct mode
  - `/help` — show command reference
- **Cross-session support.** Every `add`/`list`/`drop`/`clear` now
  accepts `--session <id-or-prefix>`. New `claude -q sessions` lists
  all known sessions with their pending counts; `list --all-sessions`
  prints queues for every session at once.
- **Windows Scheduled Task daemon (opt-in).** `claude -q scheduler
  install` registers a per-minute Windows task that sweeps all session
  queues for overdue entries and shows a Windows notification when a
  scheduled dispatch is ready but no CLI is running. `uninstall` /
  `status` / `run-once` round out the interface.

### Changed
- **Command syntax: `claude-q` → `claude -q`.** Two install paths:
  - **PowerShell users (recommended):** drop a `claude` function into
    `$PROFILE` (ready-made at `bin/claude-profile.ps1`). PowerShell
    functions take priority over `$PATH`-resolved executables, so
    routing is deterministic regardless of PATH ordering.
  - **cmd.exe users:** use `bin/claude.cmd` shim placed earlier in PATH
    than the real `claude.exe`'s directory. (Note: Windows' PATHEXT
    default prefers `.exe` over `.cmd`, so PATH ordering alone may not
    be enough — the PowerShell profile route is more reliable.)
- `claude-q` / `claude-q-add` remain as backward-compatible aliases.
- `list` output shows `@<dispatch-time>` for scheduled entries and `★`
  for priority entries.
- `monitor.snapshot()` now also reports `ready_len` (entries whose
  schedule has matured) in addition to `queue_len`.

### Fixed
- `Ctrl+Enter` / `Shift+Enter` / `Alt+Enter` now insert a newline in the
  input box instead of submitting. Plain `Enter` still submits.
- Queue UI cursor now parks inside the input box instead of below the
  box after rendering.
- `idle_detector` no longer falsely marks Claude as busy when a past-
  tense completion marker (`✻ Sautéed for 52s`) is on screen — busy
  detection now requires a spinner+ellipsis combo.

## [0.2.0] - 2026-04-24

### Changed
- **UI v2: Alt-screen queue UI.** Pressing `Ctrl+Q` now switches the
  terminal to its alt-screen buffer (`\x1b[?1049h`) and draws a clean,
  full-screen queue UI there. Exiting restores Claude's main-screen view
  exactly as it was. Solves the banner-stacking / redraw-fight problems
  of v0.1.0.

### Fixed
- Eliminated visual confusion between our `[queue]>` prompt and Claude's
  native `❯` input box.
- Removed duplicate `[claude-q] queue mode...` banners from rapid Ctrl+Q
  toggling.

## [0.1.0] - 2026-04-24

### Added
- Initial Windows-only release.
- `pywinpty`-based PTY wrapper around `claude` CLI.
- `ReadConsoleInputW` keyboard reader (bypasses IME and Windows Terminal
  ANSI-reply pollution that plagues `msvcrt.getwch`).
- Three-signal AND idle detector (prompt visible + no busy marker +
  content stable for debounce).
- Background monitor auto-dispatches queued messages when Claude
  returns to idle.
- Subcommands: `start`, `add`, `list`, `drop`, `clear`, `status`,
  `stop`, `doctor`.
- JSONL-based atomic queue store with human-inspectable state file.
- Window-title status reporter (`queue:N / mode:X / idle`).
