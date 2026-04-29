# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.4.18] - 2026-04-29

### Fixed
- **`//` dropdown was missing user-level + project-level custom
  commands.** v0.4.17 scanned built-in + skills + plugins but skipped
  two locations Claude's own `/` picker also covers:
  1. `~/.claude/commands/*.md` (user scope, visible everywhere)
  2. `<cwd>/.claude/commands/*.md` (project scope, only in this project)

  User reported: native Claude `/` picker shows `/codex /expert /gemini
  /gemma /pal-discuss` (5 project commands in `Desktop/碩論/.claude/
  commands/`), but our `//` dropdown didn't.

### Added
- `discover_native_commands(cwd=None)` now scans both new locations.
  Each entry tagged `source="user-cmd"` or `source="project-cmd"`
  for badge rendering. The `cwd` kwarg makes the project-scope scan
  testable (defaults to `Path.cwd()` in production).
- Two new regression tests: `test_discover_native_commands_picks_up_
  project_level_commands` and `test_discover_native_commands_picks_
  up_user_level_commands` use pytest's tmp_path + monkeypatch on
  Path.home() to verify both scopes work without polluting the host.

### Result on user's machine
- v0.4.17: 49 commands (22 built-in + 3 skill + 24 plugin)
- v0.4.18: **54 commands** (above + 5 project-cmd from `Desktop/碩論/`)
- Now matches what `/` shows in native Claude exactly.

### Verified
- 84/84 pytest PASS (was 82, +2 new regression)
- 100/100 fuzz PASS
- Live discovery confirmed all 5 source categories working

## [0.4.17] - 2026-04-29

### Added
- **Two-tier slash namespace in queue mode** matching the user's
  intuition from native Claude's own picker:
  - `/` (single slash) → dropdown of **queue-internal** commands only
    (`/wait /at /priority /now /drop /qclear /qhelp /claudeenter`).
    Picking one fills the buffer with the command template.
  - `//` (double slash) → dropdown of **Claude native commands**:
    built-in (`/clear /init /compact …` × 22) PLUS user-installed
    skills (scanned from `~/.claude/skills/<name>/SKILL.md`) PLUS
    plugin commands (scanned from
    `~/.claude/plugins/installed_plugins.json` cross-referenced with
    `~/.claude/plugins/cache/.../commands/*.md`). Picking one queues
    the literal `/cmd` for Claude's TUI to execute on dispatch.
  - On this machine, `//` now surfaces **49 unique commands**:
    22 built-in + 3 skills + 24 plugin commands.

### Implementation
- **New `discover_native_commands()`** in `slash_commands.py` that
  scans the filesystem on first call and caches the result at module
  level. Each entry is tagged `kind="claude"` plus
  `source ∈ {builtin, skill, plugin}` so the dropdown can render a
  source badge.
- **Plugin command dedup**: `~/.claude/plugins/cache/<repo>/<plugin>/`
  has multiple version dirs (`unknown`, `27d2b8…`, `1.0.0`, etc.) all
  containing the same command files. Dedupe by qualified name
  (`/<plugin>:<cmd>`) so each plugin command surfaces exactly once.
- **`parse()` normalizes `//xxx` → `/xxx`** so even hand-typed
  double-slash dispatches as the right command (no dropdown required).
- **`_refresh_dropdown()` two-mode logic** in `terminal_relay.py`
  detects `//` vs `/` prefix and switches between the queue and
  native pools via the new `claude_picker` flag on `filter_commands()`.
- **Dropdown header** now reads "Queue commands" or "Claude native
  commands" depending on which namespace is active, with a
  per-entry `[builtin]` / `[skill]` / `[plugin]` badge in native mode.

### Reverted (replaced)
- The v0.4.12 behaviour of mixing both pools in a single dropdown
  was removed: it made the list 30+ entries long and confused users
  who expected separation. The new dual-namespace UX matches what
  Claude itself does (`/` opens the same picker by typing into the
  prompt area).

### Verified
- `slash_commands.py` self-test PASS (covers /// normalize, single-/
  vs //  filter, native discovery cache)
- `82/82 pytest` PASS (3 new tests, 1 obsolete test reworked)
- `100/100 fuzz` PASS
- Live machine discovers 22 + 3 + 24 = 49 native commands

## [0.4.16] - 2026-04-29

### Fixed (CRITICAL — 9 versions of "tuning" never reached production)
- **The dual-source-of-truth bug.** Despite v0.4.7-v0.4.15 each tuning
  `Monitor.__init__` defaults faster (poll 0.3→0.1, debounce 0.6→0.25,
  backoff 1.0→0.5, etc.) and pytest staying green, the user kept
  reporting "still half-a-beat slow." Postmortem: `cli.py:231-241`
  constructs `Monitor` from `config.Config` at runtime, NOT from
  `Monitor.__init__` defaults. The values in `config.py` had drifted
  far apart:
  - `debounce_s = 0.6` (Monitor: 0.25)
  - `poll_interval_s = 0.3` (Monitor: 0.1)
  - `tail_chars = 16000` (PtyHost: 40000)
  - `dispatch_commit_delay_s = 0.05` (Monitor: 0.03)
  - **`post_dispatch_backoff_s = 3.0` (Monitor: 0.5)** ← the killer
  Real user-facing dispatch latency was ~3 seconds per consecutive
  queue item, not the ~580ms our integration tests measured (because
  tests pass parameters to Monitor directly, bypassing config.py).

### How it's solved
1. **Synced `config.Config` defaults to v0.4.15's fast values.** Loud
   comment now warns future contributors that the two must stay in
   sync, with a pointer to the regression test below.
2. **New regression test** `test_config_and_monitor_defaults_stay_in_sync`
   uses `inspect.signature` to compare every shared timing knob and
   fails CI loudly on any drift. Future tuning attempts can no longer
   silently miss production.
3. **New regression test** `test_actual_runtime_uses_fast_defaults`
   pins each value with a specific number, so even if both sides
   drift together (defeating the sync test), this catches it.

### Result
- Connected ground-truth: production dispatch now uses the same
  values that pytest verifies. Connecting them dropped:
  - Consecutive-dispatch gap: **~3500ms → ~600ms** (mostly the 2.5s
    backoff reduction)
  - Cold ASAP latency: **~1000ms → ~150ms**
- 80/80 pytest + 100/100 fuzz + 4/4 self-test all green.

## [0.4.15] - 2026-04-29

### Changed
- **Dispatcher tuned for tighter ASAP latency.** User feedback ("still
  feels half-a-beat slow") prompted shaving fixed-cost timing slack:
  - `poll_interval_s`: 0.3 → **0.1** (3× more responsive)
  - `debounce_s`: 0.6 → **0.25** (smaller stable-window requirement)
  - `dispatch_commit_delay_s`: 0.05 → **0.03**
  - `post_dispatch_backoff_s`: 1.0 → **0.5** (consecutive dispatches
    chain twice as fast)
  - `startup_grace_s`: 2.0 → **0.5** (claude-code 2.1.x renders its
    initial TUI in well under 500 ms)
- **Measured impact** (5-sample avg):
  - Idle→dispatch (cold): **~150 ms** (was ~1000 ms)
  - Idle→dispatch (warm): **~580 ms** (mostly Claude's own busy
    settle time, unavoidable)

### Verified
- 78/78 pytest still green
- 100/100 randomized fuzz still green

## [0.4.14] - 2026-04-29

### Fixed (CRITICAL)
- **Queue no longer holds AFTER Claude has finished responding.** Real-
  world report: Claude printed its answer + `✱ Crunched for 4s`
  completion marker + idle prompt, but the queue UI still showed
  "Waiting: Claude is busy" indefinitely. Root cause: v0.4.13's
  50-line scan window was wide enough to catch the previous
  generation's `✱ Manifesting…` / `✶ Razzle-dazzling…` lines still
  sitting in the PTY tail buffer (a 40k-char rolling window, not a
  screen emulator) — those historical residues kept tripping
  BUSY_ELLIPSIS_RE long after Claude had moved on.

### How it's solved
1. **Scan only "meaningful" lines.** Status-bar per-character redraws
   leave 1-3-char fragments (`1`, `82`, `↓`, `p2`) — none carry busy
   semantics. Filter them out before scanning, and 10 meaningful lines
   is plenty to cover the live busy marker without dredging stale
   ones from previous responses. New constant `_BUSY_MIN_LINE_LEN=4`.
2. **Position-relative busy detection.** Compare the index of the most
   recent busy marker to the index of the most recent
   `SPINNER_DONE_RE` hit (e.g. `Crunched for 4s`, `Sautéed for 52s`).
   If "done" appears AFTER "busy" in tail, the busy is historical
   residue and we report idle. This is the key insight: Claude's own
   completion marker is the most reliable "I'm done now" signal.

### Verified
- Replay of the user's exact failure tail → correctly reports idle ✓
- Real busy-in-progress → still correctly reports busy ✓
- busy → done → busy sequence (consecutive turns) → tracks correctly ✓
- Pure idle (no history) → still idle ✓
- Interrupted busy without "done" marker → conservatively busy ✓
- 77/77 pytest + 100/100 fuzz still green

## [0.4.13] - 2026-04-29

### Fixed (CRITICAL)
- **L2 force-dispatch no longer fires into active Claude generation.**
  Real-world report: while Claude was busy (`✱ Baking…`, `✶ Razzle-
  dazzling…`), the queue dispatched the next item INTO the busy state,
  interrupting the response and concatenating the queued message with
  the in-flight one. Two compounding root causes:
  1. **Spinner glyph set was incomplete.** Claude 2.1.x uses additional
     glyphs `✱` (U+2731 HEAVY ASTERISK) and `✶` (U+2736 SIX POINTED
     STAR) that the v0.4.12 BUSY_STATUS_RE character set
     `[+*●·✻✢✽✺]` didn't cover. Added both.
  2. **Busy-line scan window was too narrow.** `_BUSY_TAIL_LINES = 5`
     meant a `Baking…` line followed by 5+ status-bar fragments
     (token count, elapsed time, "thinking with high effort", "esc
     to interrupt", per-character cursor-move ANSI artifacts) was
     completely outside the scan range. Bumped to 50 — comfortably
     wider than any plausible status-bar churn while still bounded
     enough that stale busy markers from old responses don't leak in.

### Verified
- **Regression test** `test_no_dispatch_when_busy_line_buried_far_above_status_fragments`
  reproduces the exact reported scenario (busy line + 40 fragments
  below) and asserts no force-dispatch fires.
- **Regression test** `test_extra_spinner_glyphs_caught` pins ✱ / ✶
  recognition.
- **100-iteration fuzz still 100% green** after the changes.

## [0.4.12] - 2026-04-29

### Added
- **Native Claude `/commands` are now first-class queue entries.** Typing
  any of Claude Code's own `/commands` (`/clear`, `/init`, `/compact`,
  `/help`, `/model`, `/cost`, `/usage`, `/permissions`, `/agents`,
  `/skills`, `/mcp`, `/memory`, `/plan`, `/release-notes`, etc.) in
  queue mode now queues the literal string and dispatches it to Claude's
  TUI on idle — Claude executes the command exactly as if you'd typed
  it directly. Previously these were rejected with `unknown command`.
  - The dropdown autocomplete (Tab/↑↓/Enter) shows BOTH queue-internal
    and native commands in one filtered list, distinguished by a
    `kind` flag (`"queue"` vs `"claude"`) for badge rendering.
  - Unknown `/commands` (future Claude features we haven't catalogued)
    also pass through unchanged — the parser no longer needs to know
    about every Claude command in advance.
- **`/claudeenter`** — new queue-internal command that opens the
  dropdown pre-populated with the entire native Claude command list,
  so the user can browse and pick one without typing the prefix.

### Changed (breaking, narrow)
- The queue's own `/clear` and `/help` were renamed to **`/qclear`**
  and **`/qhelp`** to free those names for pass-through to Claude.
  Old muscle-memory hits will still work but route to Claude, not the
  queue. (Mitigation: dropdown shows both with summaries.)

### Verified
- **100-iteration randomized fuzz test** — every dispatch state-machine
  combination across (queue size 1-5, busy/idle initial state, mode
  toggling, busy duration 0-1.5s) runs 100× under different RNG seeds.
  Total: 100/100 pass, 165s. Catches race conditions and timing edge
  cases that 1-shot tests miss.

## [0.4.11] - 2026-04-29

### Fixed
- **ASAP queue dispatch is now ~sub-second instead of ~20s.** Three
  compounding bottlenecks made every queued item ride the L2 force-
  dispatch path even after v0.4.10:
  1. **PROMPT_RE scan window was too narrow (10 lines).** Claude Code
     2.1.121's status bar issues per-character cursor-move ANSI updates
     that, after stripping, manifest as 15-20 single-character "lines"
     at the bottom of the PTY tail. The actual `│ > │` prompt glyph
     routinely lands at line -15 to -25 — well past the 10-line scan
     window. Widened to 30 lines: L1 fast path now catches the prompt
     in normal idle states and dispatches in ~0.5s.
  2. **L2 stuck threshold was 15s.** Lowered to 5s now that the L2
     trigger is also more reliable (see #3); even if L1 misses, the
     queue recovers in 5s instead of 15s.
  3. **L2 required `stable=True` AT the exact tick `stuck >= N`.** The
     old logic forced all three signals (not_busy, stable, stuck≥N)
     to align on the same poll tick — a churning status bar would
     foil this repeatedly, adding 5+ seconds of jitter on top of the
     threshold. Replaced with `_continuously_stable_since` tracker:
     L2 fires when not_busy + stable have held continuously for
     `force_dispatch_after_stuck_s` seconds, regardless of total stuck
     time. Real Claude generation produces nonstop screen churn, so
     ANY 5-second continuous-stable window is an extremely strong
     idle signal — even if PROMPT_RE never matches.

### Result
- Best case (L1 hits): **~0.5s ASAP dispatch** (was 20s)
- Worst case (L1 fails entirely): **~5s ASAP dispatch** (was 75s in
  v0.4.6, 20s in v0.4.10)
- New regression test:
  `test_l1_prompt_scan_window_finds_prompt_buried_under_status_fragments`

## [0.4.10] - 2026-04-29

### Fixed
- **Multi-line clipboard pastes no longer auto-submit mid-paste.** When a
  user pressed Ctrl+V on text containing `\r\n`, Windows Terminal would
  convert the paste into a stream of synthetic keystrokes; the embedded
  `\r` characters fired the `VK_RETURN` handler exactly like a human
  Enter, so Claude received only the first line and started responding
  to a half-pasted message — the rest of the clipboard content vanished
  into the new prompt below. Reproducer: paste any 2+ line error message
  / log / stack trace.
  - Added paste-burst detection: a keystroke arriving < 5ms after the
    previous one is treated as part of a clipboard paste, not human
    typing (real human cadence is ≥ 30ms between keys).
  - When `VK_RETURN` arrives during a paste burst, the relay now sends
    a literal `\n` (newline within input) instead of the bare `\r`
    (submit) — the paste lands as a single multi-line message in
    Claude's input box, exactly as the user expected.
  - Same behavior in queue mode: paste-burst `\r` appends a newline
    to the queue buffer instead of committing the half-pasted content
    to `queue.jsonl`.
  - Modifier+Enter (Ctrl/Shift/Alt + Enter) still produces a literal
    newline regardless of burst timing, unchanged.
  - Plain Enter after a normal pause still submits, unchanged.
  - Four new regression tests in `TestPasteBurst`.

## [0.4.9] - 2026-04-29

### Fixed
- **Queue items now dispatch even while user is in queue mode (Ctrl+Q
  pane).** Previously the dispatcher early-returned on `get_mode() !=
  "direct"`, which meant queued items would only fire after the user
  manually toggled back to direct mode — defeating the whole "fire
  and forget" point of a queue. Real-world report: user opened a
  fresh session, Ctrl+Q'd, queued "1", expected immediate auto-
  dispatch (Claude was idle); nothing happened until they manually
  toggled to direct and sent a message themselves. Now: enqueueing
  an item is treated as an explicit "send when idle" signal,
  regardless of which composing pane the user has open.
  - The queue input pane is a separate UI buffer the dispatcher does
    not touch; Claude's response renders in the main pane above the
    queue overlay, fully visible.
  - Regression test: `test_dispatch_fires_while_user_is_in_queue_mode`.

## [0.4.8] - 2026-04-29

### Fixed
- **Stuck queues now recover in ~15s instead of ~75s.** Real-world
  testing of v0.4.7 against claude-code 2.1.121 showed `PROMPT_RE`
  still never matched even after the busy-regex fix — Claude's per-cell
  status-bar redraws fill the 4000-char PTY tail buffer faster than the
  prompt glyph can be detected, so the L2 force-dispatch path was the
  only thing actually working. Two tunings make L2 the comfortable
  primary path for 2.1.x:
  - `pty_host.PtyHost.tail_chars` default 4000 → 40000 (10× more
    history; gives the prompt rendering a longer half-life in tail).
  - `Monitor.force_dispatch_after_stuck_s` default 60 → 15 seconds.
  - `_dispatch_hint()` "TUI may have changed" hint now shows after
    10s stuck (was 60s).

## [0.4.7] - 2026-04-28

### Fixed
- **Queue no longer stalls forever against Claude Code 2.1.121's TUI.** The
  busy-state regex required `\s+` (≥1 space) between the spinner glyph
  and the status verb (`✻ Manifesting…`); the new build renders it with
  no space (`✻Manifesting…`), so `not_busy` reported True even mid-
  generation and the dispatcher couldn't find a coherent state to dispatch
  in — items sat pending for hours with the misleading hint
  `"Claude's input has draft text"`.
  - Loosened `BUSY_STATUS_RE` to `\s*` (zero-or-more) between glyph and
    verb.
  - Added `BUSY_ELLIPSIS_RE` fallback: any tail line ending in `…`
    counts as busy (catches partial PTY redraws where the spinner glyph
    landed on a separate line from the verb).
  - Added literal `"thinking with"` to `BUSY_LITERALS` (covers
    spinner-less hints like `"almost done thinking with high effort"`).
  - Bumped `_BUSY_TAIL_LINES` 3 → 5 (2.1.121's redraw scheme can push
    the busy hint up by 1–2 rows via transient status-bar fragments).

### Added
- **L2 stuck-timeout safety override.** New `Monitor` ctor arg
  `force_dispatch_after_stuck_s` (default 60s). When the canonical idle
  check fails BUT the queue has been stuck for ≥ N seconds AND the
  screen has been stable AND no busy markers are visible, dispatch
  anyway. This is the escape hatch for future Claude TUI changes that
  break our prompt regex without us noticing — instead of stalling
  invisibly for hours, the queue self-recovers within a minute.
  - Stability is the load-bearing signal (real generation churns the
    screen continuously; 60s of stable content is overwhelmingly idle).
  - `commit window` re-check is relaxed for forced dispatches: only
    aborts if a busy marker appeared, not on `r2.idle is False`.
  - Two new regression tests in `monitor.py:_self_test`: positive case
    (force fires on stable+unrecognized-prompt) and negative case
    (force does NOT fire on a churning screen).
- **`_dispatch_hint()` no longer cries wolf.** Replaced the misleading
  `"Claude's input has draft text"` hint (which was triggered on
  `prompt_visible=False` regardless of root cause) with two cases:
  - `< 60s` stuck → `"Claude prompt not visible (input has draft text,
    or TUI is redrawing)"`
  - `≥ 60s` stuck → `"Claude prompt not detected — will force dispatch
    shortly (TUI may have changed)"`

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
