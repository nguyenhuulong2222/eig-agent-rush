"""Public ledger: append-only JSONL, each entry hash-chained to the previous one.

Entry: {"seq", "ts", "type", "data", "prev_hash", "entry_hash"}
  entry_hash = SHA-256 of the canonical JSON (sorted keys, no spaces) of the entry without
  entry_hash. prev_hash of the first entry is 64 zeros.

Only the orchestrator writes it (one process, one lock). verify() recomputes the whole chain,
so any edited, removed, reordered or inserted line is detected at the first bad entry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from typing import Optional

GENESIS = "0" * 64
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_path() -> str:
    return os.environ.get("EIG_LEDGER_PATH", os.path.join(ROOT, "eig_agent", "logs", "ledger.jsonl"))


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "entry_hash"})).hexdigest()


class Ledger:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        self.lock = threading.Lock()

    def _tail(self) -> tuple:
        """(seq, entry_hash) of the last entry, or (-1, GENESIS)."""
        if not os.path.exists(self.path):
            return -1, GENESIS
        last = None
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return -1, GENESIS
        entry = json.loads(last)
        return entry["seq"], entry["entry_hash"]

    def append(self, type_: str, data: dict) -> dict:
        with self.lock:
            seq, prev = self._tail()
            entry = {"seq": seq + 1, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "type": type_, "data": data, "prev_hash": prev}
            entry["entry_hash"] = entry_hash(entry)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def entries(self, limit: Optional[int] = None) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return rows[-limit:] if limit else rows

    def verify(self) -> dict:
        return verify(self.path)


def verify(path: str) -> dict:
    """{"ok", "entries", "head", "error", "bad_seq"}: recompute every hash and link."""
    prev, count = GENESIS, 0
    if not os.path.exists(path):
        return {"ok": True, "entries": 0, "head": GENESIS, "error": None, "bad_seq": None}
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "entries": count, "head": prev, "error": f"line {lineno}: not JSON",
                        "bad_seq": count}
            if entry.get("seq") != count:
                return {"ok": False, "entries": count, "head": prev, "bad_seq": count,
                        "error": f"line {lineno}: seq {entry.get('seq')} where {count} was expected"}
            if entry.get("prev_hash") != prev:
                return {"ok": False, "entries": count, "head": prev, "bad_seq": count,
                        "error": f"seq {count}: prev_hash does not link to the previous entry"}
            if entry.get("entry_hash") != entry_hash(entry):
                return {"ok": False, "entries": count, "head": prev, "bad_seq": count,
                        "error": f"seq {count}: entry_hash does not match its content"}
            prev, count = entry["entry_hash"], count + 1
    return {"ok": True, "entries": count, "head": prev, "error": None, "bad_seq": None}
