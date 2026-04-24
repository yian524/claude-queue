"""config.py — defaults + optional TOML override from ~/.claude/claude-q.toml.

All numeric knobs live here so idle_detector / monitor / status_bar import
a single source of truth.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# Python 3.11+ has tomllib; fall back to tomli for older
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Config:
    # --- idle detection knobs ---
    debounce_s: float = 0.6          # pane must be stable this long to count as idle
    poll_interval_s: float = 0.3     # monitor wake frequency
    # Tail capacity: Claude's Ink TUI emits a LOT of ANSI per frame (cursor
    # positioning, status line refreshes, box-drawing redraws). 4 KB of
    # raw bytes is often < 40 rendered lines after ANSI stripping, which
    # can let the prompt line scroll out of our window on long answers.
    # 16 KB keeps the prompt reliably in view.
    tail_chars: int = 16000
    prompt_no_match_warn_s: float = 30.0  # degrade warning threshold

    # --- terminal relay knobs ---
    queue_toggle_key: str = "c-q"    # prompt_toolkit key name
    status_bar_enabled: bool = True
    status_bar_refresh_s: float = 0.25

    # --- dispatch safety ---
    dispatch_commit_delay_s: float = 0.05  # re-verify idle right before send
    # Minimum gap between two consecutive dispatches. Combined with
    # `saw_busy_since_dispatch` latch, this is the defense-in-depth against
    # "dispatch two queue items so fast Claude concatenates them".
    post_dispatch_backoff_s: float = 3.0

    # --- pty knobs ---
    pty_read_chunk: int = 4096
    pty_default_cols: int = 120
    pty_default_rows: int = 40

    # --- paths ---
    run_root: str = "~/.claude/run/claude-q"
    config_path: str = "~/.claude/claude-q.toml"

    def resolved_run_root(self) -> Path:
        return Path(os.path.expanduser(self.run_root))

    def resolved_config_path(self) -> Path:
        return Path(os.path.expanduser(self.config_path))


def load_config() -> Config:
    """Load defaults, overlay ~/.claude/claude-q.toml if present + valid."""
    cfg = Config()
    cfg_path = cfg.resolved_config_path()
    if not cfg_path.exists() or tomllib is None:
        return cfg
    try:
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"[claude -q] warning: failed to parse {cfg_path}: {e}", file=sys.stderr)
        return cfg
    fields = {f for f in asdict(cfg).keys()}
    overrides = {k: v for k, v in data.items() if k in fields}
    return Config(**{**asdict(cfg), **overrides})


if __name__ == "__main__":
    c = load_config()
    print(c)
