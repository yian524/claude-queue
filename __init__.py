"""claude-queue — Type-ahead FIFO queue wrapper for Claude Code CLI.

See README.md for usage. This package is deliberately small:

  queue_store    — atomic jsonl FIFO
  session        — run-id + paths
  config         — defaults + optional TOML override
  idle_detector  — three-signal AND detection of claude idle
  pty_host       — pywinpty spawn/read/write
  status_bar     — bottom-line ANSI overlay
  monitor        — background dispatcher thread
  terminal_relay — stdin<->PTY passthrough + Ctrl+Q mode toggle
  cli            — argparse entrypoint (start/add/status/stop/doctor/list/drop/clear)
"""

__version__ = "0.1.0"
