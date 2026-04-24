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


def push(path: Path, text: str, source: str = "queue-pane") -> str:
    """Append a pending entry. Returns new id. O(1) append."""
    if not text.strip():
        raise ValueError("cannot queue empty text")
    entry = QueueEntry(id=_new_id(), text=text, ts=datetime.now().isoformat(timespec="seconds"),
                       source=source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(entry.to_line() + "\n")
    return entry.id


def peek_pending(path: Path) -> Optional[QueueEntry]:
    for e in _read_all(path):
        if e.status == STATUS_PENDING:
            return e
    return None


def pop_pending(path: Path) -> Optional[QueueEntry]:
    """Flip the head pending entry to 'sent' and return it. Atomic."""
    all_entries = _read_all(path)
    for e in all_entries:
        if e.status == STATUS_PENDING:
            e.status = STATUS_SENT
            e.sent_at = datetime.now().isoformat(timespec="seconds")
            _write_all_atomic(path, all_entries)
            return QueueEntry(**asdict(e))  # return a copy
    return None


def pending_len(path: Path) -> int:
    return sum(1 for e in _read_all(path) if e.status == STATUS_PENDING)


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

        # pop ordering
        a = pop_pending(p)
        assert a is not None and a.id == id1 and a.status == STATUS_SENT
        assert pending_len(p) == 2
        b = pop_pending(p)
        assert b is not None and b.id == id2

        # drop head
        assert drop(p, id3) is True
        assert pending_len(p) == 0
        # second drop same id: already dropped, not pending -> False
        assert drop(p, id3) is False

        # clear: push 2 more then clear
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

    print("queue_store.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
