"""The owner's admin API, for the menu bar app: status, health, pause, the connected apps (list, sign out one or all), and
changing the owner passcode or the
iCloud app-specific password.

It is a separate listener on 127.0.0.1:ADMIN_PORT (off unless ADMIN_PORT is set), never mounted on the public app, so a tunnel
that forwards the public port can never reach it. Loopback alone is not trusted either: other local programs (agents among
them) can reach 127.0.0.1, so every request needs the token in DATA_DIR/admin-token (mode 600, inside the mode-700 data
folder), sent as `Authorization: Bearer <token>`. Requests from a browser (an Origin header) or with a Host other than
loopback are refused, which stops DNS rebinding. Secrets are never logged or returned."""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import importlib.metadata
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import OVERRIDABLE, OVERRIDES_FILE, Settings, owner_password_problem

log = logging.getLogger(__name__)

TOKEN_FILE = "admin-token"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_AREAS = ("mail", "calendar", "contacts", "reminders", "notes", "drive", "maps", "imessage", "health", "shortcuts", "icloud")
_HEALTH_TIMEOUT = 45


def ensure_admin_token(data_dir: str) -> str:
    """The admin token, created on first use: 32 random bytes, readable only by the owner."""
    path = os.path.join(data_dir, TOKEN_FILE)
    try:
        with open(path) as f:
            token = f.read().strip()
        if len(token) >= 32:
            os.chmod(path, 0o600)
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    _write_private(path, token + "\n")
    return token


def _write_private(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".admin-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_override(data_dir: str, key: str, value: str) -> None:
    """Store one changed credential in DATA_DIR/overrides.json (mode 600); it wins over the environment from the next start."""
    assert key in OVERRIDABLE
    path = os.path.join(data_dir, OVERRIDES_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[key] = value
    _write_private(path, json.dumps(data, indent=2) + "\n")


def _area_of(tool: str) -> str:
    head = tool.split("_", 1)[0]
    return head if head in _AREAS else "other"


def build_admin_app(s: Settings, mcp: Any, provider: Any, token: str, *, exit_fn=None) -> Starlette:
    started = time.monotonic()
    pause_file = os.path.join(s.data_dir, "paused")
    exit_fn = exit_fn or (lambda: os._exit(0))          # launchd KeepAlive or a Docker restart policy starts it again
    lock = threading.Lock()

    def refuse(request: Request) -> JSONResponse | None:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in _LOOPBACK_HOSTS or request.headers.get("origin"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        got = request.headers.get("authorization", "")
        if not (got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip().encode(), token.encode())):
            log.warning("Admin API: request without a valid token refused")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def tools_by_area() -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in mcp._tool_manager.list_tools():
            counts[_area_of(t.name)] = counts.get(_area_of(t.name), 0) + 1
        return counts

    async def status(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        bridge = getattr(mcp, "_icloud_bridge", None)
        boxes = getattr(mcp, "_icloud_outboxes", {}) or {}
        areas = tools_by_area()
        try:
            version = importlib.metadata.version("icloud-mcp-server")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return JSONResponse({
            "version": version,
            "uptime_seconds": round(time.monotonic() - started),
            "paused": os.path.exists(pause_file),
            "public_url": s.public_url,
            "tools": {"total": sum(areas.values()), "by_area": areas},
            "helper": bridge.status() if bridge is not None else None,
            "waiting_for_approval": {k: len(b.pending()) for k, b in boxes.items() if b is not None},
            "connected_apps": provider.connected_clients() if provider is not None else 0,
            "overrides_active": list(s.overrides_active),
        })

    async def health(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        check = getattr(mcp, "_icloud_health", None)
        if check is None:
            return JSONResponse({"error": "no health check"}, status_code=500)
        try:
            return JSONResponse(await asyncio.wait_for(asyncio.to_thread(check), timeout=_HEALTH_TIMEOUT))
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "error": f"The health check took longer than {_HEALTH_TIMEOUT}s."}, status_code=504)

    async def pause(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        want = bool((await body(request)).get("paused", True))
        with lock:
            if want:
                _write_private(pause_file, time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            elif os.path.exists(pause_file):
                os.unlink(pause_file)
        log.warning("Owner %s the server", "paused" if want else "resumed")
        return JSONResponse({"paused": want})

    async def sign_out_all(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        if provider is None:
            return JSONResponse({"error": "no sign-ins on this server"}, status_code=400)
        return JSONResponse({"signed_out": provider.sign_out_all()})

    async def apps(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        return JSONResponse({"apps": provider.connected_apps() if provider is not None else []})

    async def sign_out_one(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        client_id = str((await body(request)).get("id", ""))
        if provider is None or not client_id or not provider.sign_out(client_id):
            return JSONResponse({"error": "That app is not signed in."}, status_code=404)
        return JSONResponse({"signed_out": True})

    async def owner_passcode(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        value = str((await body(request)).get("passcode", ""))
        if problem := owner_password_problem(value, s.bridge_token):
            return JSONResponse({"error": problem}, status_code=400)
        save_override(s.data_dir, "owner_password", value)
        log.warning("Owner passcode changed through the admin API; it applies after a restart")
        return JSONResponse({"saved": True, "restart_required": True})

    async def app_password(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        value = str((await body(request)).get("password", "")).replace(" ", "").replace("-", "")
        value = "-".join(value[i:i + 4] for i in range(0, len(value), 4)) if len(value) == 16 else value
        if len(value.replace("-", "")) != 16:
            return JSONResponse({"error": "An app-specific password has 16 letters, like abcd-efgh-ijkl-mnop."}, status_code=400)
        from .mail import MailService
        try:                                             # prove it works before anything is saved
            c = await asyncio.to_thread(MailService(dataclasses.replace(s, app_password=value))._login)
            await asyncio.to_thread(c.logout)
        except Exception:  # noqa: BLE001 - the reason can quote the account; say only that it failed
            log.warning("Admin API: a new app-specific password was refused by iCloud; nothing saved")
            return JSONResponse({"error": "iCloud did not accept this password. Nothing was changed."}, status_code=400)
        save_override(s.data_dir, "icloud_app_password", value)
        log.warning("iCloud app-specific password changed through the admin API; it applies after a restart")
        return JSONResponse({"saved": True, "restart_required": True})

    async def restart(request: Request) -> JSONResponse:
        if r := refuse(request):
            return r
        log.warning("Owner asked for a restart through the admin API")
        threading.Timer(0.5, exit_fn).start()           # after the response has gone out
        return JSONResponse({"restarting": True})

    return Starlette(routes=[
        Route("/admin/v1/status", status, methods=["GET"]),
        Route("/admin/v1/health", health, methods=["GET"]),
        Route("/admin/v1/pause", pause, methods=["POST"]),
        Route("/admin/v1/sign-out-all", sign_out_all, methods=["POST"]),
        Route("/admin/v1/apps", apps, methods=["GET"]),
        Route("/admin/v1/apps/sign-out", sign_out_one, methods=["POST"]),
        Route("/admin/v1/owner-passcode", owner_passcode, methods=["POST"]),
        Route("/admin/v1/app-password", app_password, methods=["POST"]),
        Route("/admin/v1/restart", restart, methods=["POST"]),
    ])


def start_admin_listener(app: Starlette, port: int):
    """Serve the admin app on 127.0.0.1 only (never another address), in a background thread."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=server.run, name="admin-api", daemon=True).start()
    return server
