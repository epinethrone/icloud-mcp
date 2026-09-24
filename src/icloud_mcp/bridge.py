"""Bridge to a helper running on the owner's Mac (Reminders and Notes).

Reminders and Notes cannot be reached from a Linux server: Apple only exposes them through their own apps, which are scriptable on a
Mac. So a small helper runs on the Mac and this module hands it work:

* The helper connects OUT to a private HTTPS port of this server (never the public one) and long-polls for jobs, so the Mac opens no
  listening port and works from wherever it can reach the server (home network or VPN).
* Jobs are structured requests for a FIXED list of operations (OPS). The Mac never receives script text to run, only an operation
  name and validated arguments, and it refuses anything not in its own copy of the list.
* The channel is TLS with a self-signed certificate that the helper pins by fingerprint, plus a bearer token.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import Settings

log = logging.getLogger("icloud_mcp.bridge")

ONLINE_WINDOW = 45          # seconds after the last poll during which the helper counts as online (polls last <= 30 s)
MAX_BODY = 1_048_576        # largest request body accepted on the bridge port
_MAX_FAILURES, _FAILURE_WINDOW = 20, 900


class BridgeError(Exception):
    pass


# ---------------------------------------------------------------------------
# The operations the Mac may be asked to perform. The Mac helper carries its own copy of this table (tests keep them identical).
# arg spec: name -> (type, required, max). Types: str, int, bool, iso (ISO 8601 date-time or None).
# ---------------------------------------------------------------------------
OPS: dict[str, dict[str, tuple[str, bool, int]]] = {
    # Reminders
    "reminder_lists": {},
    "reminders_list": {"list": ("str", False, 200), "list_id": ("str", False, 200), "query": ("str", False, 200), "refresh": ("bool", False, 0),
                       "limit": ("int", False, 200)},
    "reminder_create": {"title": ("str", True, 500), "list": ("str", False, 200), "list_id": ("str", False, 200), "notes": ("str", False, 20000), "due": ("iso", False, 40),
                        "priority": ("int", False, 9)},
    "reminder_update": {"id": ("str", True, 500), "title": ("str", False, 500), "notes": ("str", False, 20000), "due": ("iso", False, 40),
                        "clear_due": ("bool", False, 0), "priority": ("int", False, 9)},
    "reminder_complete": {"id": ("str", True, 500), "completed": ("bool", False, 0)},
    "reminder_delete": {"id": ("str", True, 500)},
    # Notes
    "note_folders": {},
    "notes_list": {"folder": ("str", False, 200), "query": ("str", False, 200), "search_body": ("bool", False, 0), "limit": ("int", False, 100)},
    "note_read": {"id": ("str", True, 500), "max_chars": ("int", False, 100000)},
    "note_create": {"title": ("str", True, 500), "body": ("str", False, 100000), "folder": ("str", False, 200)},
    "note_delete": {"id": ("str", True, 500), "title": ("str", True, 500)},
    "note_folder_create": {"name": ("str", True, 200), "account": ("str", False, 200), "parent_id": ("str", False, 500)},
    "note_move": {"id": ("str", True, 500), "title": ("str", True, 500), "folder_id": ("str", False, 500), "folder": ("str", False, 200)},
    # iCloud Drive (paths are relative to the Drive; see ops/drive.py)
    "drive_list": {"path": ("str", False, 1000), "include_hidden": ("bool", False, 0), "limit": ("int", False, 1000)},
    "drive_search": {"query": ("str", True, 200), "path": ("str", False, 1000), "limit": ("int", False, 200)},
    "drive_info": {"path": ("str", True, 1000)},
    "drive_read": {"path": ("str", True, 1000), "max_chars": ("int", False, 200000), "offset": ("int", False, 50000000)},
    "drive_write": {"path": ("str", True, 1000), "content": ("str", False, 500000), "overwrite": ("bool", False, 0)},
    "drive_mkdir": {"path": ("str", True, 1000)},
    "drive_move": {"path": ("str", True, 1000), "to": ("str", True, 1000)},
    "drive_trash": {"path": ("str", True, 1000)},
    # Shortcuts (only names on BOTH the server's SHORTCUTS_ALLOW and the Mac's own shortcuts-allow.txt run; see ops/shortcut.py)
    "shortcut_run": {"name": ("str", True, 200), "input": ("str", False, 20000)},
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def _iso_ok(value: str) -> bool:
    """A well-formed ISO 8601 date or date-time that is also a REAL date and time (no 2026-13-45, 2026-02-30 or 25:00)."""
    m = _ISO.match(value)
    if not m:
        return False
    try:
        dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return False
    if m[4] is not None and not (int(m[4]) <= 23 and int(m[5]) <= 59 and (m[6] is None or int(m[6]) <= 59)):
        return False
    return not (m[7] and m[7] != "Z" and (int(m[7][1:3]) > 23 or int(m[7][-2:]) > 59))


def validate_args(op: str, args: dict[str, Any] | None) -> dict[str, Any]:
    spec = OPS.get(op)
    if spec is None:
        raise BridgeError(f"Unknown Mac operation '{op}'.")
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise BridgeError("Arguments must be an object.")
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if key not in spec:
            raise BridgeError(f"Operation '{op}' does not take '{key}'.")
        kind, _, limit = spec[key]
        if value is None:
            clean[key] = None
        elif kind == "str":
            if not isinstance(value, str) or len(value) > limit:
                raise BridgeError(f"'{key}' must be text of at most {limit} characters.")
            clean[key] = value
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise BridgeError(f"'{key}' must be a whole number between 0 and {limit}.")
            clean[key] = value
        elif kind == "bool":
            if not isinstance(value, bool):
                raise BridgeError(f"'{key}' must be true or false.")
            clean[key] = value
        elif kind == "iso":
            if not isinstance(value, str) or len(value) > 40 or not _iso_ok(value):
                raise BridgeError(f"'{key}' must be an ISO 8601 date or date-time such as 2026-09-21 or 2026-09-21T15:00:00+02:00.")
            clean[key] = value
        else:  # pragma: no cover - a mistake in OPS itself
            raise BridgeError(f"Unsupported argument type {kind}.")
    missing = [k for k, (_, required, _) in spec.items() if required and clean.get(k) is None]
    if missing:
        raise BridgeError(f"Operation '{op}' needs: {', '.join(missing)}.")
    return clean


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    op: str
    args: dict[str, Any]
    deadline: float
    state: str = "queued"                 # queued | running | done | cancelled
    ok: bool = False
    result: Any = None
    error: str = ""
    event: threading.Event = field(default_factory=threading.Event)


class MacBridge:
    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._cond = threading.Condition()
        self._queue: list[Job] = []
        self._jobs: dict[str, Job] = {}
        self.last_seen: float | None = None
        self.agent: dict[str, str] = {}

    # -- state --------------------------------------------------------------------
    def _online_locked(self) -> bool:
        now = time.time()
        if any(j.state == "running" and j.deadline > now for j in self._jobs.values()):
            return True                                        # busy running a job counts as present
        return self.last_seen is not None and now - self.last_seen < ONLINE_WINDOW

    def status(self) -> dict[str, Any]:
        with self._cond:
            ago = None if self.last_seen is None else round(time.time() - self.last_seen)
            return {"online": self._online_locked(), "last_seen_seconds_ago": ago, "helper": dict(self.agent),
                    "jobs_waiting": sum(1 for j in self._queue if j.state == "queued")}

    def _offline_message(self) -> str:
        if self.last_seen is None:
            when = "has not connected since the server started"
        else:
            mins = (time.time() - self.last_seen) / 60
            when = f"was last seen {mins:.0f} minute(s) ago" if mins >= 1 else "was last seen moments ago"
        return (f"The Mac helper {when}. Reminders and Notes work through the owner's Mac, which must be on, awake and connected to "
                "the home network or VPN. Tell the user instead of retrying repeatedly.")

    # -- called by tools (worker threads) ---------------------------------------------
    def call(self, op: str, args: dict[str, Any] | None = None) -> Any:
        clean = validate_args(op, args)
        with self._cond:
            if not self._online_locked():
                raise BridgeError(self._offline_message())
            job = Job(id=secrets.token_urlsafe(9), op=op, args=clean, deadline=time.time() + self.timeout)
            self._jobs[job.id] = job
            self._queue.append(job)
            self._cond.notify_all()
        if not job.event.wait(self.timeout):
            with self._cond:
                if job.state in ("queued", "running"):
                    job.state = "cancelled"
                    if job in self._queue:
                        self._queue.remove(job)
            raise BridgeError(f"The Mac helper did not answer within {self.timeout}s. The operation may still complete on the Mac, "
                              "so check before repeating a change.")
        if not job.ok:
            raise BridgeError(job.error or "The Mac helper reported an error.")
        return job.result

    # -- called by the bridge HTTP app -----------------------------------------------------
    def _touch(self, meta: dict[str, str]) -> None:
        self.last_seen = time.time()
        if meta:
            self.agent = meta

    def next_job(self, meta: dict[str, str], wait: float) -> Job | None:
        end = time.monotonic() + wait
        with self._cond:
            self._touch(meta)
            while True:
                now = time.time()
                while self._queue:
                    job = self._queue.pop(0)
                    if job.state == "queued" and job.deadline > now:
                        job.state = "running"
                        return job
                remaining = end - time.monotonic()
                if remaining <= 0:
                    self._touch(meta)
                    self._forget_old_locked()
                    return None
                self._cond.wait(remaining)

    def complete(self, job_id: str, ok: bool, result: Any, error: str) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.state != "running":
                return False
            job.state, job.ok, job.result, job.error = "done", bool(ok), result, str(error or "")[:500]
            self.last_seen = time.time()
            job.event.set()
            return True

    def _forget_old_locked(self) -> None:
        if len(self._jobs) > 200:
            keep = sorted(self._jobs.values(), key=lambda j: j.deadline)[-100:]
            self._jobs = {j.id: j for j in keep}


# ---------------------------------------------------------------------------
# TLS: a self-signed certificate created once and pinned by the helper
# ---------------------------------------------------------------------------
def ensure_tls(data_dir: str, extra_names: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """Return (cert_path, key_path, sha256_fingerprint_hex). Created on first use; the fingerprint is stable across restarts."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = d / "bridge-cert.pem", d / "bridge-key.pem"
    if not (cert_path.exists() and key_path.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "icloud-mcp-bridge")])
        sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
        for n in extra_names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(n)))
            except ValueError:
                sans.append(x509.DNSName(n))
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .sign(key, hashes.SHA256()))
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    (d / "bridge_fingerprint.txt").write_text(fingerprint + "\n")
    return str(cert_path), str(key_path), fingerprint


# ---------------------------------------------------------------------------
# The private HTTPS app the Mac helper talks to
# ---------------------------------------------------------------------------
def build_bridge_app(bridge: MacBridge, s: Settings) -> Starlette:
    failures: list[float] = []

    def locked_out() -> bool:
        now = time.time()
        failures[:] = [t for t in failures if now - t < _FAILURE_WINDOW]
        return len(failures) >= _MAX_FAILURES

    def denied(request: Request) -> Response | None:
        if locked_out():
            return JSONResponse({"error": "too many failed attempts"}, status_code=429)
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), s.bridge_token.encode()):
            failures.append(time.time())
            log.warning("bridge: request refused (bad or missing token)")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def read_json(request: Request) -> Any:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise BridgeError("request body too large")
        return json.loads(body or b"{}")

    async def ping(request: Request) -> Response:
        return denied(request) or JSONResponse({"ok": True, "server_time": int(time.time()), "bridge": bridge.status()})

    async def poll(request: Request) -> Response:
        if (bad := denied(request)) is not None:
            return bad
        try:
            body = await read_json(request)
            wait = min(max(float(body.get("wait", 25)), 0.0), 30.0)
        except (BridgeError, ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        meta = {"host": str(body.get("host", ""))[:80], "version": str(body.get("version", ""))[:20], "os": str(body.get("os", ""))[:40]}
        job = await asyncio.to_thread(bridge.next_job, meta, wait)
        if job is None:
            return Response(status_code=204)
        return JSONResponse({"id": job.id, "op": job.op, "args": job.args, "seconds": max(1, int(job.deadline - time.time()))})

    async def result(request: Request) -> Response:
        if (bad := denied(request)) is not None:
            return bad
        try:
            body = await read_json(request)
            job_id, ok = str(body["job"]), bool(body["ok"])
        except (BridgeError, ValueError, TypeError, KeyError, AttributeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return JSONResponse({"accepted": bridge.complete(job_id, ok, body.get("result"), body.get("error", ""))})

    return Starlette(routes=[Route("/bridge/ping", ping, methods=["GET"]), Route("/bridge/poll", poll, methods=["POST"]),
                             Route("/bridge/result", result, methods=["POST"])])


def start_bridge_listener(app: Starlette, port: int, certfile: str, keyfile: str, host: str = "0.0.0.0"):
    """Serve the bridge app over TLS on its own port, in a background thread. Returns the uvicorn server (for tests)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile,
                                           log_level="warning", access_log=False))
    threading.Thread(target=server.run, name="mac-bridge", daemon=True).start()
    return server
