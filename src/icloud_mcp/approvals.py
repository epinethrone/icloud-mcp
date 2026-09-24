"""Owner approval pages for outgoing mail queued by the MCP tools.

An MCP client (possibly steered by injected text in an email) can only *queue* a message. It is delivered only after the
owner opens /outbox in a browser, types the owner password, reviews the exact message and presses Approve. No page here
is reachable by, or returns anything useful to, a caller who lacks the password.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .auth import OwnerOAuthProvider
from .config import Settings
from .mail import MailService

log = logging.getLogger(__name__)

_TOKEN_TTL = 600        # seconds an Approve/Discard button stays valid after the password was entered
_BODY_PREVIEW = 20000

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}

_CSS = """
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#666;--th:#555;--line:#bbb;--quiet:#e8e8ed;--code:#f5f5f7;--accent:#0071e3;--err:#c00;--warn:#8a5300;--shadow:#0002}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--muted:#98989d;--th:#aeaeb2;--line:#48484a;--quiet:#3a3a3c;--code:#2c2c2e;--accent:#0a84ff;--err:#ff6961;--warn:#ffb340;--shadow:#0000}}
body{font:16px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:1rem}
main{max-width:46rem;margin:0 auto}
.card{background:var(--card);padding:1.25rem;border-radius:14px;margin:1rem 0;box-shadow:0 2px 20px var(--shadow)}
h1{font-size:1.25rem;margin:.2rem 0 .6rem} h2{font-size:1.05rem;margin:0 0 .6rem}
table{border-collapse:collapse;width:100%} th{text-align:left;vertical-align:top;padding:.15rem .8rem .15rem 0;white-space:nowrap;color:var(--th)}
td{padding:.15rem 0;word-break:break-word;font-family:ui-monospace,Menlo,monospace;font-size:.9rem}
pre{background:var(--code);padding:.8rem;border-radius:8px;white-space:pre-wrap;word-break:break-word;max-height:26rem;overflow:auto;font-size:.9rem}
input[type=password]{width:100%;box-sizing:border-box;padding:.7rem;font-size:1rem;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;margin:.5rem 0 1rem}
button{padding:.7rem 1.2rem;font-size:1rem;border-radius:8px;border:0;cursor:pointer;margin-right:.5rem}
.ok{background:var(--accent);color:#fff}.no{background:var(--quiet);color:var(--text)}.err{color:var(--err)}.warn{color:var(--warn)}.muted{color:var(--muted);font-size:.9rem}
"""


def _page(title: str, inner: str, status: int = 200) -> Response:
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>{inner}</main></body></html>")
    return HTMLResponse(body, status_code=status, headers=_HEADERS)


def _login_form(error: str = "", status: int = 200) -> Response:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page("Outgoing mail approval", f"""<div class="card"><h1>Outgoing mail approval</h1>
<p>Enter the owner password to review messages your agents have queued. Nothing is sent until you approve it here.</p>{err}
<form method="post" action="/outbox"><label for="pw">Owner password</label>
<input id="pw" type="password" name="password" autocomplete="current-password" autofocus required>
<button class="ok" type="submit">Review queue</button></form></div>""", status)


def _row(label: str, value: str) -> str:
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(value) if value else '<span class=muted>(none)</span>'}</td></tr>" if value or label in ("Cc", "Bcc") else ""


def register_outbox_routes(mcp: Any, provider: OwnerOAuthProvider, settings: Settings, mail: MailService) -> None:
    key = secrets.token_bytes(32)  # per-process: buttons issued before a restart simply stop working

    def _tok(item_id: str, sha: str, action: str, exp: int) -> str:
        return hmac.new(key, f"{item_id}|{sha}|{action}|{exp}".encode(), hashlib.sha256).hexdigest()

    def _queue_page(note: str = "") -> Response:
        items = mail.outbox.pending()
        exp = int(time.time()) + _TOKEN_TTL
        parts = [f'<div class="card"><h1>Outgoing mail waiting for your approval</h1>{note}'
                 f'<p class="muted">{len(items)} waiting. Review the exact recipients and text below; this is what will be sent.</p></div>']
        for q in items:
            d = mail.describe_queued(q)
            atts = "".join(f"<li>{html.escape(str(a.get('filename')))} ({html.escape(str(a.get('content_type')))}, {a.get('size')} bytes)</li>"
                           for a in d["attachments"]) or ""
            body = d["body"] if len(d["body"]) <= _BODY_PREVIEW else d["body"][:_BODY_PREVIEW] + "\n\n[... preview truncated; the full message is sent]"
            mins = max(1, int((q.expires_at - time.time()) / 60))
            forms = ""
            for action, label, cls in (("approve", "Approve and send", "ok"), ("discard", "Discard", "no")):
                forms += (f'<form method="post" action="/outbox/act" style="display:inline"><input type="hidden" name="id" value="{html.escape(q.id)}">'
                          f'<input type="hidden" name="action" value="{action}"><input type="hidden" name="exp" value="{exp}">'
                          f'<input type="hidden" name="tok" value="{_tok(q.id, q.sha256, action, exp)}">'
                          f'<button class="{cls}" type="submit">{label}</button></form>')
            parts.append(f"""<div class="card"><h2>{html.escape(d['subject'] or '(no subject)')}</h2><table>
{_row('From', d['from'])}{_row('To', d['to'])}{_row('Cc', d['cc'])}{_row('Bcc', d['bcc'])}
{_row('Will be delivered to', ', '.join(d['envelope_recipients']))}
</table>{('<p class=warn>Attachments:</p><ul>' + atts + '</ul>') if atts else ''}
<pre>{html.escape(body)}</pre><p class="muted">Expires in about {mins} min. Approve only if you asked your agent to send this.</p>{forms}</div>""")
        if not items:
            parts.append('<div class="card"><p>Nothing is waiting.</p></div>')
        return _page("Outgoing mail approval", "".join(parts))

    @mcp.custom_route("/outbox", methods=["GET"])
    async def outbox_get(_: Request) -> Response:
        return _login_form()

    @mcp.custom_route("/outbox", methods=["POST"])
    async def outbox_post(request: Request) -> Response:
        form = await request.form()
        if not provider.login_allowed():
            return _login_form("Too many failed attempts. Try again in 15 minutes.", 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), settings.owner_password.encode()):
            provider.record_failure()
            log.warning("Failed owner-password attempt on /outbox")
            return _login_form("Incorrect password.", 401)
        return _queue_page()

    @mcp.custom_route("/outbox/act", methods=["POST"])
    async def outbox_act(request: Request) -> Response:
        form = await request.form()
        item_id, action = str(form.get("id", "")), str(form.get("action", ""))
        try:
            exp = int(str(form.get("exp", "0")))
        except ValueError:
            exp = 0
        tok = str(form.get("tok", ""))
        q = next((x for x in mail.outbox.pending() if x.id == item_id), None)
        if action not in ("approve", "discard") or exp < time.time() or q is None:
            return _page("Outgoing mail approval", '<div class="card"><p class="err">That request has expired or the message is no longer waiting.</p>'
                         '<p><a href="/outbox">Back</a></p></div>', 400)
        if not hmac.compare_digest(tok.encode(), _tok(q.id, q.sha256, action, exp).encode()):
            log.warning("Rejected /outbox/act with an invalid token")
            return _page("Outgoing mail approval", '<div class="card"><p class="err">Invalid request. Re-enter the owner password.</p>'
                         '<p><a href="/outbox">Back</a></p></div>', 403)
        if action == "discard":
            mail.outbox.claim(q.id)
            log.info("Owner discarded queued message %s", q.id)
            return _queue_page('<p class="ok-note">Discarded.</p>')
        try:
            result = await asyncio.to_thread(mail.release, q.id)
        except Exception as e:  # noqa: BLE001  -- release() re-queues on any failure, so the owner can retry or discard
            log.warning("Release of %s failed: %s", q.id, e)
            return _queue_page(f'<p class="err">Not sent (still queued): {html.escape(str(e))}</p>')
        log.info("Owner approved and sent queued message %s", q.id)
        extra = f" {html.escape(result['warning'])}" if result.get("warning") else ""
        return _queue_page(f'<p><b>Sent</b> to {html.escape(", ".join(result.get("recipients", [])))}.{extra}</p>')
