"""queue_store.py — append-only JSONL FIFO queue.

Design
------
* Each entry is one JSON object on its own line.
* Status transitions are monotonic: pending -> sent | dropped.
* push() is O(1) append (atomic on NTFS for <4 KiB writes).
* pop_pending() rewrites the file via a temp file + os.replace() for atomicity.
* File is human-inspectable and crash-safe: a torn write only corrupts the
  last line; parse() skips corrupt lines with a warning.

Entry schema
------------
    {
      "id":     str,    # ULID-ish (timestamp+random hex)
      "text":   str,    # the message to inject into claude
      "ts":     str,    # ISO-8601 created-at
      "status": str,    # pending | sent | dropped
      "sent_at": str|None,
      "source":  str    # "queue-pane" | "claude-q-add" | "test"
    }
"""
from __future__ import annotations

import json
import os
import random
import string
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_DROPPED = "dropped"


def _new_id() -> str:
    """Timestamp-prefixed random hex; sortable by creation."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    rand = "".join(random.choices(string.hexdigits.lower()[:16], k=6))
    return f"{ts}-{rand}"


@dataclass
class QueueEntry:
    id: str
    text: str
    ts: str
    status: str = STATUS_PENDING
    sent_at: Optional[str] = None
    source: str = "queue-pane"
    # --- scheduling fields (added v0.3.0) ---
    dispatch_at: Optional[str] = None   # ISO timestamp; if set, dispatcher waits until this time
    priority: int = 0                   # higher = dispatched first within the eligible set

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _read_all(path: Path) -> List[QueueEntry]:
    if not path.exists():
        return []
    out: List[QueueEntry] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(QueueEntry(**d))
            except Exception:
                # corrupt torn line: skip silently (monitor will also log once)
                continue
    return out


def _write_all_atomic(path: Path, entries: Iterable[QueueEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # write to temp in same directory so os.replace() is atomic on NTFS
    fd, tmp_name = tempfile.mkstemp(prefix=".queue-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(e.to_line() + "\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def push(
    path: Path,
    text: str,
    source: str = "queue-pane",
    dispatch_at: Optional[str] = None,
    priority: int = 0,
) -> str:
    """Append a pending entry. Returns new id. O(1) append.

    Parameters
    ----------
    dispatch_at : ISO-8601 timestamp. If set, the monitor will not
        dispatch this entry until `now >= dispatch_at` (and Claude is
        also idle).
    priority : higher wins within the set of dispatch-eligible entries.
        Default 0. /priority slash command sets this to 100.
    """
    if not text.strip():
        raise ValueError("cannot queue empty text")
    entry = QueueEntry(
        id=_new_id(),
        text=text,
        ts=datetime.now().isoformat(timespec="seconds"),
        source=source,
        dispatch_at=dispatch_at,
        priority=priority,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(entry.to_line() + "\n")
    return entry.id


def _is_dispatch_eligible(e: QueueEntry, now_iso: str) -> bool:
    """Is this pending entry ready to be dispatched at now?"""
    if e.status != STATUS_PENDING:
        return False
    if e.dispatch_at is None:
        return True
    return e.dispatch_at <= now_iso  # ISO-8601 strings sort lexicographically


def _rank(e: QueueEntry) -> tuple:
    """Dispatch-order key: higher priority first, then earlier dispatch_at,
    then earlier creation ts. Use negative priority so Python's tuple
    sort (ascending) yields the correct order.
    """
    return (-e.priority, e.dispatch_at or "", e.ts)


def peek_pending(path: Path, now_iso: Optional[str] = None) -> Optional[QueueEntry]:
    """Return the NEXT entry that's eligible for dispatch at `now_iso`.

    Selection: among all pending + dispatch-eligible entries, pick the
    one with highest priority, earliest dispatch_at, earliest ts.
    """
    if now_iso is None:
        now_iso = datetime.now().isoformat(timespec="seconds")
    eligible = [e for e in _read_all(path) if _is_dispatch_eligible(e, now_iso)]
    if not eligible:
        return None
    return min(eligible, key=_rank)


def pop_pending(path: Path, now_iso: Optional[str] = None) -> Optional[QueueEntry]:
    """Flip the next dispatch-eligible entry to 'sent' and return it.

    Atomic. Respects scheduling (dispatch_at) and priority.
    """
    if now_iso is None:
        now_iso = datetime.now().isoformat(timespec="seconds")
    all_entries = _read_all(path)
    eligible = [e for e in all_entries if _is_dispatch_eligible(e, now_iso)]
    if not eligible:
        return None
    chosen = min(eligible, key=_rank)
    chosen.status = STATUS_SENT
    chosen.sent_at = datetime.now().isoformat(timespec="seconds")
    _write_all_atomic(path, all_entries)
    return QueueEntry(**asdict(chosen))


def pending_len(path: Path) -> int:
    """Total pending entries, regardless of schedule."""
    return sum(1 for e in _read_all(path) if e.status == STATUS_PENDING)


def dispatch_ready_len(path: Path, now_iso: Optional[str] = None) -> int:
    """Pending entries whose schedule has matured (dispatchable right now)."""
    if now_iso is None:
        now_iso = datetime.now().isoformat(timespec="seconds")
    return sum(1 for e in _read_all(path) if _is_dispatch_eligible(e, now_iso))


def list_all(path: Path) -> List[QueueEntry]:
    return _read_all(path)


def drop(path: Path, entry_id: str) -> bool:
    """Mark one pending entry as dropped. Returns True if found."""
    all_entries = _read_all(path)
    changed = False
    for e in all_entries:
        if e.id == entry_id and e.status == STATUS_PENDING:
            e.status = STATUS_DROPPED
            changed = True
            break
    if changed:
        _write_all_atomic(path, all_entries)
    return changed


def clear(path: Path) -> int:
    """Mark all pending as dropped. Returns count affected."""
    all_entries = _read_all(path)
    n = 0
    for e in all_entries:
        if e.status == STATUS_PENDING:
            e.status = STATUS_DROPPED
            n += 1
    if n:
        _write_all_atomic(path, all_entries)
    return n


# ------------------------------- self-test -------------------------------

def _self_test() -> int:
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        p = Path(td) / "queue.jsonl"

        # empty
        assert pending_len(p) == 0
        assert peek_pending(p) is None
        assert pop_pending(p) is None

        # push 3
        id1 = push(p, "first")
        id2 = push(p, "second")
        id3 = push(p, "third")
        assert pending_len(p) == 3
        head = peek_pending(p)
        assert head is not None and head.id == id1

        # pop ordering (FIFO among equal priority, no schedule)
        a = pop_pending(p)
        assert a is not None and a.id == id1 and a.status == STATUS_SENT
        assert pending_len(p) == 2
        b = pop_pending(p)
        assert b is not None and b.id == id2

        # drop head
        assert drop(p, id3) is True
        assert pending_len(p) == 0
        assert drop(p, id3) is False

        # clear
        push(p, "four")
        push(p, "five")
        assert pending_len(p) == 2
        n = clear(p)
        assert n == 2
        assert pending_len(p) == 0

        # corrupt line tolerance
        with p.open("a", encoding="utf-8") as f:
            f.write("{ this is not valid json\n")
        push(p, "after-corrupt")
        assert pending_len(p) == 1

        # empty text rejected
        try:
            push(p, "   ")
            assert False, "should have raised"
        except ValueError:
            pass

    # --- scheduling tests ---
    with _tf.TemporaryDirectory() as td:
        p = Path(td) / "queue.jsonl"
        # future dispatch_at should not be eligible
        far_future = "2099-01-01T00:00:00"
        near_past = "2000-01-01T00:00:00"
        id_future = push(p, "future", dispatch_at=far_future)
        id_past = push(p, "past", dispatch_at=near_past)
        id_no_sched = push(p, "unscheduled")
        # pending count includes all
        assert pending_len(p) == 3
        # but only 2 are dispatch-ready (past-scheduled + no-schedule)
        assert dispatch_ready_len(p) == 2
        # pop should pick earliest eligible (past-scheduled comes before no-sched
        # because its dispatch_at '2000-01-01...' < empty string? No — empty
        # string sorts first. So unscheduled pops first.)
        first = pop_pending(p)
        assert first is not None and first.id == id_no_sched, first
        second = pop_pending(p)
        assert second is not None and second.id == id_past
        # future entry remains
        assert pending_len(p) == 1
        assert dispatch_ready_len(p) == 0
        remaining = peek_pending(p)
        assert remaining is None  # not eligible yet

    # --- priority tests ---
    with _tf.TemporaryDirectory() as td:
        p = Path(td) / "queue.jsonl"
        push(p, "normal-1", priority=0)
        push(p, "normal-2", priority=0)
        id_prio = push(p, "VIP", priority=100)
        # VIP should pop first despite being pushed last
        first = pop_pending(p)
        assert first is not None and first.id == id_prio and first.text == "VIP"

    print("queue_store.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
