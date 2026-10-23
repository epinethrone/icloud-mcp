"""The activity log: who changed what, and when, on this server.

Every write an agent makes through a tool, every approval and discard on the dashboard, every owner sign-in (and failed
attempt), new and removed passkeys, signed-out apps and changed settings land here as one JSON line. It records names and
addresses, never message bodies, passwords or tokens. The log lives in DATA_DIR/audit.jsonl (mode 600) and keeps
AUDIT_KEEP_DAYS days (90 by default).

Each entry carries the hash of the entry before it, so a line changed or removed after it was written breaks the chain, and
the dashboard says so. That is tamper evidence for the file, not a defence against someone who can rewrite the whole file:
the hashes are not signed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE = "audit.jsonl"
_GENESIS = "0" * 64
_MAX_TEXT = 200          # longest single value kept in an entry (a subject, an event title)


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT - 1] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:25]]
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in list(value.items())[:25]}
    return value


def _digest(prev: str, entry: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "h"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    """Append-only, one process. `record` never raises: a full disk must not turn into a failed send or a failed sign-in."""

    def __init__(self, data_dir: str, keep_days: int = 90, enabled: bool = True):
        self.path = Path(data_dir) / FILE
        self.keep_days = max(1, keep_days)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last = self._tail_hash()
        self._pruned_at = 0.0

    def _tail_hash(self) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = [x for x in f.read().splitlines() if x.strip()]
            return json.loads(lines[-1])["h"] if lines else _GENESIS
        except (OSError, ValueError, KeyError, IndexError):
            return _GENESIS

    def record(self, action: str, *, actor: str, via: str = "", summary: str = "", ok: bool = True, **details: Any) -> None:
        """actor: 'owner', an app's name, or 'server'. via: how the owner signed in (passkey name, 'password'), or the client id."""
        if not self.enabled:
            return
        try:
            with self._lock:
                entry = {"t": round(time.time(), 3), "action": action, "actor": _clip(actor), "ok": bool(ok)}
                if via:
                    entry["via"] = _clip(via)
                if summary:
                    entry["summary"] = _clip(summary)
                if details:
                    entry["details"] = _clip({k: v for k, v in details.items() if v not in (None, "", [], {})})
                entry["prev"] = self._last
                entry["h"] = _digest(self._last, entry)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
                os.chmod(self.path, 0o600)
                self._last = entry["h"]
                if time.time() - self._pruned_at > 3600:
                    self._prune()
        except Exception as e:  # noqa: BLE001  -- logging must never break the action it records
            log.warning("Could not write the activity log: %s", type(e).__name__)

    def _prune(self) -> None:
        """Drop entries older than keep_days. The first kept entry still names the hash of the one before it; verify() starts there."""
        self._pruned_at = time.time()
        cutoff = time.time() - self.keep_days * 86400
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        keep = []
        for line in lines:
            try:
                if json.loads(line).get("t", 0) >= cutoff:
                    keep.append(line)
            except ValueError:
                keep.append(line)         # an unreadable line stays, so verify() can report it
        if len(keep) == len(lines):
            return
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in keep))
        os.replace(tmp, self.path)

    def entries(self, limit: int = 200, kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """The newest entries first. kinds filters by action prefix ('approval.', 'signin.')."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if kinds and not str(e.get("action", "")).startswith(kinds):
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def verify(self) -> dict[str, Any]:
        """Walk the chain. ok=False names the first line that does not match: changed, removed or inserted after writing."""
        try:
            lines = [x for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return {"ok": True, "entries": 0, "since": None}
        prev, since = None, None
        for n, line in enumerate(lines, 1):
            try:
                e = json.loads(line)
            except ValueError:
                return {"ok": False, "entries": len(lines), "broken_at": n, "since": since}
            if prev is None:                 # the oldest kept entry: its predecessor may have been pruned
                prev, since = e.get("prev", _GENESIS), e.get("t")
            if e.get("prev") != prev or _digest(prev, e) != e.get("h"):
                return {"ok": False, "entries": len(lines), "broken_at": n, "since": since}
            prev = e["h"]
        return {"ok": True, "entries": len(lines), "since": since}
