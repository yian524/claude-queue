"""session.py — session id, run-dir paths, ACTIVE pointer.

Run dir layout:
  ~/.claude/run/claude-q/<session_id>/
    session.json  — {pid, started_at, claude_cmd, cols, rows}
    queue.jsonl   — append-only FIFO
    status.json   — {queue_len, mode, idle, last_reason, drift_detected}
    monitor.log   — debug log
"""
from __future__ import annotations

import json
import os
import random
import string
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import load_config


def new_session_id() -> str:
    """Format: YYYYMMDDTHHMMSS-<6 random lowercase hex>."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.hexdigits.lower()[:16], k=6))
    return f"{ts}-{suffix}"


def run_root() -> Path:
    root = load_config().resolved_run_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_dir(sid: str) -> Path:
    d = run_root() / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_pointer_path() -> Path:
    return run_root() / "ACTIVE"


def set_active(sid: str) -> None:
    active_pointer_path().write_text(sid, encoding="utf-8")


def clear_active() -> None:
    p = active_pointer_path()
    if p.exists():
        p.unlink()


def active_session() -> Optional[str]:
    p = active_pointer_path()
    if not p.exists():
        return None
    sid = p.read_text(encoding="utf-8").strip()
    return sid or None


def require_active() -> str:
    sid = active_session()
    if not sid:
        raise RuntimeError("No active claude-q session. Run `claude-q start` first.")
    return sid


@dataclass
class SessionState:
    sid: str
    pid: int
    started_at: str
    claude_cmd: str
    cols: int
    rows: int
    dry_run: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "SessionState":
        return cls(**json.loads(text))


def write_session(state: SessionState) -> None:
    path = session_dir(state.sid) / "session.json"
    path.write_text(state.to_json(), encoding="utf-8")


def read_session(sid: str) -> Optional[SessionState]:
    path = session_dir(sid) / "session.json"
    if not path.exists():
        return None
    try:
        return SessionState.from_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_sessions() -> list[str]:
    root = run_root()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


if __name__ == "__main__":
    # self-test
    sid = new_session_id()
    print("sid:", sid)
    print("dir:", session_dir(sid))
    set_active(sid)
    assert active_session() == sid
    clear_active()
    assert active_session() is None
    print("session.py self-test: PASS")
