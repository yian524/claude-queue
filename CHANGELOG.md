# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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

### Fixed
- `Ctrl+Enter` / `Shift+Enter` / `Alt+Enter` now insert a newline in the
  input box instead of submitting. Plain `Enter` still submits.
- Queue UI cursor now parks inside the input box instead of below the
  box after rendering.

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
