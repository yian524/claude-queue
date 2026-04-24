"""status_bar.py — claude-q status shown in the terminal window title.

Why the title bar: Claude owns the full screen and uses ANSI cursor control
to redraw its TUI. Any in-screen overlay (bottom row, sidebar) races with
Claude's redraws. Writing to the terminal title via OSC 0/2 escape sequences
is a zero-conflict channel: it updates the title bar (Windows Terminal,
ConEmu, iTerm2 all honour it) without touching any screen cell.

We also write human-readable JSON to `status.json` in the run dir so
`claude-q status` and the queue-pane TUI can render richer info.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# OSC 0 sets both icon and window title; 2 only window title.
# Terminator: BEL (\a). Most modern terminals also accept ST (\x1b\\).
_TITLE_ESC = "\x1b]0;{text}\x07"


def set_window_title(text: str, stream=sys.stdout) -> None:
    """Write the OSC title sequence to a stream (default stdout)."""
    try:
        stream.write(_TITLE_ESC.format(text=text))
        stream.flush()
    except Exception:
        pass


def write_status_json(run_dir: Path, payload: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / ".status.tmp"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, run_dir / "status.json")
    except Exception:
        pass


class StatusBar:
    """Periodic status-title updater.

    Caller registers a `provider` callable that returns the latest status dict,
    and start()s the refresh thread. stop() cleanly returns terminal title.
    """

    def __init__(
        self,
        run_dir: Path,
        provider: Callable[[], dict],
        refresh_s: float = 0.25,
        enabled: bool = True,
    ):
        self.run_dir = run_dir
        self.provider = provider
        self.refresh_s = refresh_s
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------- lifecycle -------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="status-bar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # restore a benign title so the user's terminal isn't stuck on "claude-q"
        try:
            set_window_title("claude")
        except Exception:
            pass

    # ------------------------- internals -------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.refresh_s):
            try:
                payload = self.provider()
            except Exception:
                continue
            if self.enabled:
                title = self._format_title(payload)
                set_window_title(title)
            write_status_json(self.run_dir, payload)

    @staticmethod
    def _format_title(p: dict) -> str:
        parts = ["claude-q"]
        q = p.get("queue_len", 0)
        parts.append(f"queue:{q}")
        mode = p.get("mode", "direct")
        parts.append(f"mode:{mode}")
        idle = p.get("idle", None)
        if idle is True:
            parts.append("idle")
        elif idle is False:
            parts.append("busy")
        if p.get("drift_detected"):
            parts.append("⚠drift")
        return " │ ".join(parts)


# ------------------------------- self-test -------------------------------

def _self_test() -> int:
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        run_dir = Path(td)
        state = {"queue_len": 0, "mode": "direct", "idle": True}

        def _prov():
            return state

        bar = StatusBar(run_dir, _prov, refresh_s=0.05, enabled=False)
        bar.start()
        state["queue_len"] = 3
        state["mode"] = "queue"
        state["idle"] = False
        state["drift_detected"] = True
        time.sleep(0.2)
        bar.stop()

        sp = run_dir / "status.json"
        assert sp.exists(), "status.json should be written"
        data = json.loads(sp.read_text("utf-8"))
        assert data["queue_len"] == 3
        assert data["mode"] == "queue"

        title = StatusBar._format_title(data)
        assert "queue:3" in title and "mode:queue" in title and "⚠drift" in title

    print("status_bar.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
