# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
