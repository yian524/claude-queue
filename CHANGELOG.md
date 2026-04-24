# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.4.6] - 2026-04-24

### Fixed
- **Relay loop no longer dies on a single bad keystroke.** A user hitting
  `/0` + Enter in queue mode hit a code path that raised
  `list assignment index out of range`; the previous loop caught the
  exception at the TOP level, so one key error killed the whole input
  relay and the session became unresponsive. Now each `_handle_key`
  call is wrapped in its own try/except — a bad key is isolated, logged,
  and the loop continues.

### Added
- **`~/.claude/run/claude-q/relay_errors.log`** — tracebacks for both
  per-key errors and fatal relay crashes are written here, along with
  enough state (mode / cursor / buffer / dropdown / key event) to
  reproduce. Written unconditionally (not gated behind
  `CLAUDE_Q_DEBUG`).
- **`claude -q log --errors`** — dump that error log without hunting
  for the file. The default `claude -q log` listing also shows a
  yellow notice when the error log is non-empty.

## [0.4.5] - 2026-04-24

### Added
- **`claude -q log` subcommand** for inspecting `monitor.log` when
  diagnosing stuck / slow dispatches. Supersedes the standalone
  `bin/claude-q-log.ps1` script (which was never on PATH so users
  couldn't actually invoke it).
  - `claude -q log` — list 10 most recent sessions with log sizes
  - `claude -q log --latest` — dump the newest session's log
  - `claude -q log --session <id or prefix>` — dump a specific session
  - `claude -q log --since 18:00` — dump all sessions modified after HH:MM today
  - `--tail N` on any dump mode to show only the last N lines

## [0.4.2] - 2026-04-24

### Fixed
- **v0.4.1's arrow-key fix was dead code.** The `_handle_key` flow had
  an early-return for `k.vt is not None` (arrow / function keys) that
  fired in queue mode too, returning before the new cursor-navigation
  code could run. Moved the queue-mode Left/Right/Home/End/Delete/
  Backspace handlers ABOVE the VT passthrough block so they actually
  execute. In-queue editing now works as intended.

## [0.4.1] - 2026-04-24

### Added
- **Queue-mode input is now fully editable.** Previously the left/right
  arrow keys were ignored so the only way to fix a typo was to
  backspace all the way from the end. Now in the alt-screen queue UI:
  - `←` / `→` move the cursor one char within the buffer
  - `Home` / `End` jump to start / end of buffer
  - `Delete` removes the char AT the cursor
  - `Backspace` removes the char BEFORE the cursor
  - Typing inserts AT the cursor position (not always at end)
  - CJK characters move the visual cursor 2 columns per step

### Implementation
- New `_cursor_pos` state on `TerminalRelay` tracks logical position in
  `_queue_buf` (0..len). Reset to 0 on mode entry and on exit.
- `_render_queue_ui` computes `cursor_col` from the visual width of
  `buf_display[:cursor_visible_pos]` so the terminal cursor lands at
  the correct column even mid-buffer.

## [0.4.0] - 2026-04-24

### Fixed
- **Two queued entries dispatching back-to-back before Claude noticed the
  first one, concatenating into a single garbled prompt.** Root cause:
  `post_dispatch_backoff_s` was 1.0s but Claude Code's busy marker
  (`✻`, `Swirling…`, etc.) can take >1s to appear in the PTY tail after
  we write to it. During that window the idle detector still saw the
  PRE-dispatch idle state, so the monitor happily dispatched the next
  ready entry too.
  - Added a `saw_busy_since_dispatch` latch on `MonitorState`. After
    each dispatch, the latch is armed (False). It clears the instant
    `is_idle()` returns False (Claude confirmed it's processing).
    Subsequent dispatches are blocked until the latch clears.
  - 15-second stale-latch release: if Claude never goes busy after our
    dispatch (perhaps the payload was empty or got lost), we release
    the latch so the queue doesn't stall forever.
  - `post_dispatch_backoff_s` bumped 1.0s → 3.0s as belt-and-braces.

### Added
- CJK-aware cursor positioning in the queue-mode input box. Previously
  each Chinese character counted as 1 column for cursor math but rendered
  as 2 columns, so the cursor drifted left of the actual end of input
  whenever the buffer contained CJK. New `_visual_width()` helper uses
  `unicodedata.east_asian_width` to count W/F chars as 2 cols.

## [0.3.8] - 2026-04-24

### Fixed
- **Phantom empty-`>` prompts accumulating across Ctrl+Q cycles.**
  Final root cause: v0.3.4's clear-then-replay strategy (`\x1b[H\x1b[2J`
  before flushing `paused_buf`) pushed the cleared content into the
  terminal's scrollback on every Ctrl+Q exit. After a few cycles the
  scrollback looked like:
  ```
  > <empty>
  > <user msg 1>
  <response 1>
  > <empty>        <-- phantom, from v0.3.4 clear push
  > <user msg 2>
  <response 2>
  > <empty>        <-- another phantom
  ```
  **New strategy: just drop buffered PTY bytes during alt-screen mode.**
  On exit, the terminal's native `\x1b[?1049l` restores the pre-alt
  main screen. Claude's next frame (from a keystroke or dispatched
  queue entry) redraws the full TUI cleanly from scratch. Trade-off:
  if Claude was mid-streaming when user entered alt-screen, that
  output is not re-displayed, but it remains in Claude's internal
  conversation state and will be included in subsequent responses.

## [0.3.7] - 2026-04-24

### Added
- Queue-mode UI now shows a **dispatch hint line** explaining why the
  head entry is (or isn't) dispatching:
  - `Next: 'xyz...' fires in 3m 12s` for scheduled entries
  - `Next: ASAP — Claude is idle, dispatching soon`
  - `Waiting: Claude's input has draft text (submit or clear it)` when
    the real blocker is user-typed draft
  - `Waiting: Claude is busy` when mid-response

### Fixed
- `claude -q doctor` failed with `module 'idle_detector' has no
  attribute 'PROMPT_RE'` because v0.3.3 split the regex into
  `PROMPT_RE_END` + `PROMPT_RE_LINE` but doctor wasn't updated. Now
  probes both.

## [0.3.5] - 2026-04-24

### Changed
- **Queue confirmation moves from main screen to window title.** Every
  `[claude-q] queued id=...` confirmation and `/now sent` message now
  updates the terminal window title (OSC 0) instead of printing on the
  main screen. This keeps the cursor on Claude's own input prompt and
  eliminates the extra green text line that used to stack up next to
  Claude's chat history.
- Error messages (`push failed`, etc.) still print on the main screen
  because the user needs to see them.

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
