"""Single-owner OAuth 2.1 authorization server for the MCP endpoint.

claude.ai custom connectors speak OAuth (with dynamic client registration + PKCE). This
provider lets exactly one person -- whoever knows MCP_OWNER_PASSWORD -- authorize a client.
The iCloud app-specific password never leaves the server; clients only ever hold
short-lived bearer tokens for *this* server, which you can revoke by deleting
``$DATA_DIR/oauth_state.json``.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

log = logging.getLogger(__name__)

SCOPE = "icloud"
_PENDING_TTL = 600
_CODE_TTL = 300
_MAX_FAILURES = 10
_FAILURE_WINDOW = 900
_MAX_CLIENTS = 100     # registrations are open (that is how claude.ai connects), so the file they land in must stay bounded


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def with_query(uri: str, **params: str | None) -> str:
    parts = urlsplit(uri)
    q = dict(parse_qsl(parts.query))
    q.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit(parts._replace(query=urlencode(q)))


class OwnerOAuthProvider:
    """Implements mcp's OAuthAuthorizationServerProvider protocol with JSON-file persistence."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.path = Path(settings.data_dir) / "oauth_state.json"
        self._lock = threading.RLock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}    # sha256(token) -> record
        self.refresh: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.failures: list[float] = []
        # Diagnostics only, never persisted: fingerprints (hashes) of tokens that were rotated, and log rate limiting.
        self._rotated: dict[str, float] = {}
        self._last_logged: dict[str, tuple[float, int]] = {}
        self._load()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001
            log.exception("Could not read %s; starting with empty OAuth state", self.path)
            return
        self.clients = data.get("clients", {})
        self.access = data.get("access", {})
        self.refresh = data.get("refresh", {})
        self._prune()

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".oauth-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"clients": self.clients, "access": self.access, "refresh": self.refresh}, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def _prune(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if v["expires_at"] > now}
        self.refresh = {k: v for k, v in self.refresh.items() if v["expires_at"] > now}
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        self.pending = {k: v for k, v in self.pending.items() if v["expires_at"] > now}

    # -- diagnostics (never log token values; only short client ids, reasons and ages) ---------
    @staticmethod
    def _cid(client_id: str | None) -> str:
        return (client_id or "?")[:8]

    def _note_rotated(self, hashes: list[str]) -> None:
        now = time.time()
        for h in hashes:
            self._rotated[h] = now
        if len(self._rotated) > 1000:
            cutoff = now - 24 * 3600
            self._rotated = {k: v for k, v in self._rotated.items() if v > cutoff}

    def _rotated_ago(self, token_hash: str) -> float | None:
        when = self._rotated.get(token_hash)
        return None if when is None else time.time() - when

    def _log_limited(self, key: str, level: int, msg: str, *args: Any) -> None:
        """Log at most once a minute per key (unauthenticated callers must not be able to flood the log)."""
        now = time.time()
        last, suppressed = self._last_logged.get(key, (0.0, 0))
        if now - last < 60:
            self._last_logged[key] = (last, suppressed + 1)
            return
        log.log(level, msg + (f" (+{suppressed} more like this in the last minute)" if suppressed else ""), *args)
        self._last_logged[key] = (now, 0)

    # -- client registration ----------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rec = self.clients.get(client_id)
        if not rec:
            self._log_limited("unknown-client", logging.WARNING, "oauth: request from unknown client_id %s (not registered here)", self._cid(client_id))
        return OAuthClientInformationFull.model_validate(rec) if rec else None

    def _redirect_ok(self, uri: str) -> bool:
        allowed = self.s.allowed_redirect_hosts
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
        if "*" in allowed:
            return parts.scheme in ("https", "http")
        if host not in allowed:
            return False
        return parts.scheme == "https" or (parts.scheme == "http" and host in ("localhost", "127.0.0.1"))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self._redirect_ok(str(uri)):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"Redirect host not allowed by OAUTH_ALLOWED_REDIRECT_HOSTS: {urlsplit(str(uri)).hostname}",
                )
        with self._lock:
            if client_info.client_id not in self.clients and len(self.clients) >= _MAX_CLIENTS:
                self._prune_clients()
                if len(self.clients) >= _MAX_CLIENTS:
                    raise RegistrationError("invalid_client_metadata",
                                            "Too many registered clients. The owner can revoke old ones by deleting oauth_state.json.")
            self.clients[client_info.client_id] = client_info.model_dump(mode="json")
            self._save()

    def _prune_clients(self) -> None:
        """Drop registrations that hold no token and no pending request, oldest first, down to half the cap. A client the
        owner approved keeps its tokens and is never touched."""
        self._prune()
        live = ({v["client_id"] for v in self.access.values()} | {v["client_id"] for v in self.refresh.values()}
                | {v["client_id"] for v in self.pending.values()} | {c.client_id for c in self.codes.values()})
        idle = sorted((cid for cid in self.clients if cid not in live), key=lambda cid: self.clients[cid].get("client_id_issued_at") or 0)
        for cid in idle[:max(0, len(self.clients) - _MAX_CLIENTS // 2)]:
            del self.clients[cid]

    # -- authorization (consent page lives in register_routes) -------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pid = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self.pending[pid] = {"client_id": client.client_id, "client_name": client.client_name, "params": params,
                                 "expires_at": time.time() + _PENDING_TTL}
        return f"{self.s.public_url}/login?{urlencode({'p': pid})}"

    def approve(self, pid: str) -> str | None:
        """Turn a pending request into an authorization code; returns the redirect URL."""
        with self._lock:
            pend = self.pending.pop(pid, None)
            if not pend or pend["expires_at"] < time.time():
                return None
            params: AuthorizationParams = pend["params"]
            code = secrets.token_urlsafe(32)
            self.codes[code] = AuthorizationCode(
                code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + _CODE_TTL, client_id=pend["client_id"],
                code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource, subject="owner",
            )
            return with_query(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, pid: str) -> str | None:
        with self._lock:
            pend = self.pending.pop(pid, None)
        if not pend:
            return None
        params: AuthorizationParams = pend["params"]
        return with_query(str(params.redirect_uri), error="access_denied", state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        if not code:
            reason = "code not recognised (already used, expired and cleaned up, or never issued here)"
        elif code.client_id != client.client_id:
            reason = f"code was issued to a different client ({self._cid(code.client_id)})"
        else:
            reason = "code expired"
        log.warning("oauth: authorization_code refused for client %s: %s", self._cid(client.client_id), reason)
        return None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        with self._lock:
            if self.codes.pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "authorization code already used or expired")
            return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource, "authorization_code")

    # -- tokens ----------------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str], resource: str | None, grant: str = "?") -> OAuthToken:
        now = int(time.time())
        access, refresh, pair = secrets.token_urlsafe(48), secrets.token_urlsafe(48), secrets.token_hex(8)
        self.access[_h(access)] = {"client_id": client_id, "scopes": scopes, "resource": resource, "pair": pair,
                                   "expires_at": now + self.s.access_token_ttl}
        self.refresh[_h(refresh)] = {"client_id": client_id, "scopes": scopes, "resource": resource, "pair": pair,
                                     "expires_at": now + self.s.refresh_token_ttl}
        self._prune()
        self._save()
        log.info("oauth: issued tokens to client %s via %s (%d refresh tokens stored)", self._cid(client_id), grant, len(self.refresh))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.s.access_token_ttl,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        h = _h(refresh_token)
        rec = self.refresh.get(h)
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] <= time.time():
            cid = self._cid(client.client_id)
            if not rec:
                ago = self._rotated_ago(h)
                if ago is not None:
                    log.warning("oauth: refresh refused for client %s: this refresh token was already used %.0fs ago and rotated. "
                                "Another client or a retry is holding a stale copy of the sign-in.", cid, ago)
                else:
                    log.warning("oauth: refresh refused for client %s: token not recognised (never issued here, revoked, cleaned up "
                                "after expiry, or rotated before the last server restart). %d refresh tokens are stored.", cid, len(self.refresh))
            elif rec["client_id"] != client.client_id:
                log.warning("oauth: refresh refused: presented by client %s but issued to client %s", cid, self._cid(rec["client_id"]))
            else:
                log.warning("oauth: refresh refused for client %s: refresh token expired %.1f h ago", cid, (time.time() - rec["expires_at"]) / 3600)
            return None
        return RefreshToken(token=refresh_token, client_id=rec["client_id"], scopes=rec["scopes"],
                            expires_at=int(rec["expires_at"]), resource=rec.get("resource"), subject="owner")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._lock:
            rec = self.refresh.get(_h(refresh_token.token))
            if not rec:
                raise TokenError("invalid_grant", "refresh token revoked or expired")
            granted = scopes or rec["scopes"]
            if not set(granted) <= set(rec["scopes"]):
                raise TokenError("invalid_scope", "requested scope exceeds original grant")
            self._note_rotated([_h(refresh_token.token)] + [k for k, v in self.access.items() if v["pair"] == rec["pair"]])
            self._drop_pair(rec["pair"])  # rotate
            return self._issue(client.client_id, granted, rec.get("resource"), "refresh_token")

    async def load_access_token(self, token: str) -> AccessToken | None:
        h = _h(token)
        rec = self.access.get(h)
        if not rec or rec["expires_at"] <= time.time():
            if rec:
                self._log_limited("access-expired", logging.INFO, "oauth: access token of client %s expired %.0fs ago (client should refresh)",
                                  self._cid(rec["client_id"]), time.time() - rec["expires_at"])
            elif self._rotated_ago(h) is not None:
                self._log_limited("access-rotated", logging.WARNING, "oauth: access token refused: it was replaced by a refresh %.0fs ago "
                                  "(a client is still using the old copy)", self._rotated_ago(h))
            else:
                self._log_limited("access-unknown", logging.INFO, "oauth: access token not recognised (never issued here, expired and cleaned up, "
                                  "revoked, or from before the last restart)")
            return None
        return AccessToken(token=token, client_id=rec["client_id"], scopes=rec["scopes"], expires_at=int(rec["expires_at"]),
                           resource=rec.get("resource"), subject="owner")

    def _drop_pair(self, pair: str) -> None:
        self.access = {k: v for k, v in self.access.items() if v["pair"] != pair}
        self.refresh = {k: v for k, v in self.refresh.items() if v["pair"] != pair}

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            rec = self.access.get(_h(token.token)) or self.refresh.get(_h(token.token))
            if rec:
                self._drop_pair(rec["pair"])
                self._save()

    # -- brute-force protection ------------------------------------------------------------
    def login_allowed(self) -> bool:
        now = time.time()
        self.failures = [t for t in self.failures if now - t < _FAILURE_WINDOW]
        return len(self.failures) < _MAX_FAILURES

    def record_failure(self) -> None:
        self.failures.append(time.time())


# ---------------------------------------------------------------------------
# Consent page
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>Authorize iCloud connector</title>
<style>
:root{{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#666;--th:#555;--line:#bbb;--quiet:#e8e8ed;--code:#f5f5f7;--accent:#0071e3;--err:#c00;--warn:#8a5300;--shadow:#0002}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--muted:#98989d;--th:#aeaeb2;--line:#48484a;--quiet:#3a3a3c;--code:#2c2c2e;--accent:#0a84ff;--err:#ff6961;--warn:#ffb340;--shadow:#0000}}}}
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;display:grid;place-items:center;min-height:100vh}}
main{{background:var(--card);padding:2rem;border-radius:14px;max-width:26rem;width:calc(100% - 2rem);box-shadow:0 2px 20px var(--shadow)}}
h1{{font-size:1.25rem;margin:0 0 .5rem}} p{{margin:.5rem 0}} .err{{color:var(--err)}} .muted{{color:var(--muted);font-size:.9rem}}
input[type=password]{{width:100%;box-sizing:border-box;padding:.7rem;font-size:1rem;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;margin:.5rem 0 1rem}}
button{{padding:.7rem 1.2rem;font-size:1rem;border-radius:8px;border:0;cursor:pointer;margin-right:.5rem}}
.ok{{background:var(--accent);color:#fff}} .no{{background:var(--quiet);color:var(--text)}}
</style></head><body><main>
<h1>Authorize access to your iCloud</h1>
<p><b>{client}</b> is requesting read and write access to your iCloud Mail and Calendar through this server.</p>
<p class="muted">After you approve, it is sent back to <b>{redirect}</b> (client id {cid}). Approve only if you started this
connection yourself a moment ago; anyone can ask for this page, and the password is what decides.</p>
{error}
<form method="post" action="/login">
<input type="hidden" name="p" value="{pid}">
<label for="pw">Owner password</label>
<input id="pw" type="password" name="password" autocomplete="current-password" autofocus required>
<button class="ok" name="action" value="approve" type="submit">Approve</button>
<button class="no" name="action" value="deny" type="submit" formnovalidate>Deny</button>
</form></main></body></html>"""

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
}


def _page(provider: OwnerOAuthProvider, pid: str, error: str = "", status: int = 200) -> Response:
    pend = provider.pending.get(pid)
    if not pend or pend["expires_at"] < time.time():
        return HTMLResponse("<p>This authorization request has expired. Return to the app and try connecting again.</p>",
                            status_code=400, headers=_HEADERS)
    name = pend.get("client_name") or "An application"
    redirect = urlsplit(str(pend["params"].redirect_uri)).hostname or "?"
    body = _PAGE.format(client=html.escape(name), pid=html.escape(pid), redirect=html.escape(redirect),
                        cid=html.escape(OwnerOAuthProvider._cid(pend.get("client_id"))),
                        error=f'<p class="err">{html.escape(error)}</p>' if error else "")
    return HTMLResponse(body, status_code=status, headers=_HEADERS)


def register_routes(mcp: Any, provider: OwnerOAuthProvider, settings: Settings) -> None:
    @mcp.custom_route("/login", methods=["GET"])
    async def login_get(request: Request) -> Response:
        return _page(provider, request.query_params.get("p", ""))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_post(request: Request) -> Response:
        form = await request.form()
        pid = str(form.get("p", ""))
        action = str(form.get("action", "approve"))
        if action == "deny":
            url = provider.deny(pid)
            return RedirectResponse(url, status_code=302, headers=_HEADERS) if url else _page(provider, pid)
        if not provider.login_allowed():
            return _page(provider, pid, "Too many failed attempts. Try again in 15 minutes.", 429)
        supplied = str(form.get("password", ""))
        if not hmac.compare_digest(supplied.encode(), settings.owner_password.encode()):
            provider.record_failure()
            log.warning("Failed owner-password attempt")
            return _page(provider, pid, "Incorrect password.", 401)
        url = provider.approve(pid)
        if not url:
            return _page(provider, pid)
        return RedirectResponse(url, status_code=302, headers=_HEADERS)
