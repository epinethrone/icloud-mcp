"""The owner's dashboard: one place in the browser for approvals, activity, connected apps, passkeys and the server's state.

It replaces typing the owner password on every visit to /outbox with a signed-in session. Sign-in is a passkey (WebAuthn,
see passkeys.py) or the owner password; the owner can turn the password off once a passkey exists. Steps that weaken
protection (removing a passkey, changing the password, signing apps out) ask again when the last sign-in is more than five
minutes old.

Sessions: a random token in a cookie (`HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefix on https), stored only as a
SHA-256 hash in DATA_DIR/sessions.json (mode 600), so a restart keeps you signed in and a copy of the file signs nobody in.
They end after DASHBOARD_SESSION_MINUTES without use (30) and after 12 hours in any case. Every form carries a per-session
CSRF token and every POST must come from this server's own origin.

Pages are rendered on the server. The only script is /dashboard/passkey.js, served from here (CSP `script-src 'self'`), which
calls the browser's passkey API. Everything else works without it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import importlib.metadata
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import passkeys as P
from .approvals import flagged
from .audit import AuditLog
from .config import Settings, owner_password_problem

log = logging.getLogger(__name__)

SESSIONS_FILE = "sessions.json"
_ABSOLUTE = 12 * 3600          # a session never lasts longer than this
_STEP_UP = 300                 # seconds a sign-in counts as recent for sensitive steps
_BODY_PREVIEW = 20000
_STATIC = Path(__file__).parent / "static"

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",       # browsers then send our own Origin on form posts (no-referrer makes it "null")
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self'; "
                                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
}


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------------------------------
class Sessions:
    def __init__(self, data_dir: str, idle_minutes: int):
        self.path = Path(data_dir) / SESSIONS_FILE
        self.idle = max(5, idle_minutes) * 60
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text())
            self._items = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        self._items = {k: v for k, v in self._items.items()
                       if now - v.get("last", 0) < self.idle and now - v.get("created", 0) < _ABSOLUTE}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sessions-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._items, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def create(self, via: str) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        rec = {"created": now, "last": now, "auth_at": now, "via": via, "csrf": secrets.token_urlsafe(24)}
        with self._lock:
            self._prune()
            self._items[_h(token)] = rec
            self._save()
        return token, rec

    def get(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            self._prune()
            rec = self._items.get(_h(token))
            if rec is None:
                return None
            if time.time() - rec["last"] > 60:          # write at most once a minute while browsing
                rec["last"] = time.time()
                self._save()
            return rec

    def reauth(self, token: str, via: str) -> None:
        with self._lock:
            if rec := self._items.get(_h(token)):
                rec["auth_at"], rec["via"] = time.time(), via
                self._save()

    def drop(self, token: str) -> None:
        with self._lock:
            if self._items.pop(_h(token), None) is not None:
                self._save()

    def drop_all(self) -> None:
        with self._lock:
            self._items.clear()
            self._save()


# ---------------------------------------------------------------------------------------------------------------------
# Approval sources: one interface for queued mail, iMessages and (with ALLOW_CALENDAR_INVITES=approve) calendar changes
# ---------------------------------------------------------------------------------------------------------------------
class ApprovalSource:
    kind = ""
    badge = ""
    approve_label = "Approve"

    def pending(self) -> list[Any]:
        raise NotImplementedError

    def describe(self, q: Any, context: Any = None) -> dict[str, Any]:
        """{'title', 'badge', 'rows': [(label, value)], 'text', 'notes': [str], 'warnings': [str], 'needs_override': bool}"""
        raise NotImplementedError

    def contexts(self, items: list[Any]) -> dict[str, Any]:
        return {}

    def release(self, q: Any) -> str:
        """Do it; return a one-line result for the page. Raises on failure (and puts the item back)."""
        raise NotImplementedError

    def discard(self, q: Any) -> None:
        raise NotImplementedError


class MailApprovals(ApprovalSource):
    kind, badge, approve_label = "mail", "Email", "Send"

    def __init__(self, mail: Any):
        self.mail = mail

    def pending(self):
        return self.mail.outbox.pending()

    def contexts(self, items):
        return self.mail.approval_contexts(items, prune=True) if items else {}

    def describe(self, q, context=None):
        d = self.mail.describe_queued(q, context)
        body = d["body"] if len(d["body"]) <= _BODY_PREVIEW else d["body"][:_BODY_PREVIEW] + "\n\n[... preview truncated; the full message is sent]"
        rows = [("From", d["from"]), ("To", d["to"]), ("Cc", d["cc"]), ("Bcc", d["bcc"]),
                ("Will be delivered to", ", ".join(d["envelope_recipients"]))]
        warnings = list((d.get("original") or {}).get("safety_warnings") or [])
        if d.get("agent_added_recipients"):
            warnings.append("A recipient's address was added to a contact by an agent, not by you: "
                            + ", ".join(d["agent_added_recipients"]))
        notes = []
        if orig := d.get("original"):
            frm = ", ".join(f"{a.get('name', '')} <{a.get('email', '')}>".strip() for a in orig.get("from") or [] if isinstance(a, dict))
            notes.append("In reply to: " + ("(the original message could not be read)" if orig.get("unavailable")
                                            else f"{orig.get('subject') or '(no subject)'} from {frm or '?'}"))
        if d["attachments"]:
            notes.append("Attachments: " + ", ".join(f"{a.get('filename')} ({a.get('size')} bytes)" for a in d["attachments"]))
        return {"title": d["subject"] or "(no subject)", "badge": "Email reply" if d.get("original") else "Email",
                "rows": [(k, v) for k, v in rows if v], "text": body, "notes": notes, "warnings": warnings,
                "needs_override": bool(flagged(d))}

    def release(self, q):
        r = self.mail.release(q.id)
        return f"Sent to {', '.join(r.get('recipients', []))}." + (f" {r['warning']}" if r.get("warning") else "")

    def discard(self, q):
        self.mail.outbox.claim(q.id)


class IMessageApprovals(ApprovalSource):
    kind, badge, approve_label = "imessage", "iMessage", "Send"

    def __init__(self, imessage: Any, box: Any):
        self.imessage, self.box = imessage, box

    def pending(self):
        return self.box.pending()

    def describe(self, q, context=None):
        d = self.imessage.describe_queued(q)
        warnings = ["A group conversation: everyone in it receives this."] if d["group"] else []
        return {"title": f"iMessage to {d['to'] or d['chat_id'] or ''}", "badge": "iMessage",
                "rows": [("Conversation", d["chat_id"] or "")] if d["chat_id"] else [], "text": d["text"],
                "notes": ["Sent from your Mac once you approve."], "warnings": warnings, "needs_override": False}

    def release(self, q):
        r = self.imessage.release(self.box, q.id)
        word = {"sent": "Sent", "failed": "Not delivered", "unconfirmed": "Handed to Messages (not yet confirmed)"}.get(r.get("status"), str(r.get("status")))
        return f"{word}: iMessage to {r.get('to', '')}. {r.get('note', '')}".strip()

    def discard(self, q):
        self.box.claim(q.id)


# ---------------------------------------------------------------------------------------------------------------------
# Page frame and components
#
# Every page follows the same structure, so nothing floats:
#   top bar (phones) or sidebar (from 860 px): the app, this server's address, its state
#   page head: title and one line of context
#   cards: a head (title, at most one status chip, never a button), a body (rows, text, fields), a foot (all of the
#   card's buttons, right-aligned, the main action last; full width on phones)
#   row actions (remove a passkey, sign out an app) sit at the right end of their row, all the same size
# ---------------------------------------------------------------------------------------------------------------------
_CSS = """
:root{color-scheme:light dark;--bg:#f2f2f7;--card:#fff;--text:#1d1d1f;--muted:#636366;--line:#d1d1d6;--quiet:#e5e5ea;--code:#f2f2f7;
--accent:#0071e3;--accent-text:#0066cc;--info:#e8f0fe;--info-text:#0b4a8f;--ok-bg:#e3f5e8;--ok:#1a7f37;--warn-bg:#fdf1dc;--warn:#8a5300;
--err-bg:#fde8e8;--err:#c00;--badge:#d70015;--bar:#f9f9fb;--shadow:#0000000d}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--muted:#98989d;--line:#38383a;--quiet:#2c2c2e;--code:#2c2c2e;
--accent:#0a84ff;--accent-text:#4da3ff;--info:#0a2a4d;--info-text:#9ecbff;--ok-bg:#0f2e1a;--ok:#3fb950;--warn-bg:#33240a;--warn:#ffb340;
--err-bg:#3a1414;--err:#ff6961;--badge:#ff453a;--bar:#111113;--shadow:#0000}}
*{box-sizing:border-box}
body{font:16px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;-webkit-text-size-adjust:100%}
a{color:var(--accent-text)}
h1,h2,h3{margin:0;text-wrap:balance}
p{margin:0}
.wrap{display:flex;min-height:100vh}
main{flex:1;min-width:0;max-width:52rem;padding:0 16px calc(96px + env(safe-area-inset-bottom,0px))}
.stack{display:flex;flex-direction:column;gap:12px}
/* top bar (phones) */
.topbar{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top,0px)) 16px 10px;
background:var(--bar);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;min-width:0;flex:1}
.brand img{width:28px;height:28px;border-radius:7px;flex-shrink:0}
.brand-text{display:flex;flex-direction:column;min-width:0;line-height:1.2}
.brand-name{font-weight:600;font-size:15px}
.brand-host{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* page head */
.page-head{padding:20px 0 14px;display:flex;flex-direction:column;gap:4px}
.page-head h1{font-size:30px;font-weight:700;letter-spacing:-.01em}
.lede{color:var(--muted);font-size:15px}
/* cards */
.card{background:var(--card);border-radius:14px;box-shadow:0 1px 4px var(--shadow);overflow:hidden}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 16px 0}
.card-head h2{font-size:17px;font-weight:600}
.card-body{padding:10px 16px 14px;display:flex;flex-direction:column;gap:10px}
.card-foot{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;padding:12px 16px;border-top:1px solid var(--quiet)}
.card-foot .btn{flex:1 1 8rem}
.card-foot form{display:contents}
.list{display:flex;flex-direction:column}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:46px;padding:6px 0;border-bottom:1px solid var(--quiet)}
.row:last-child{border-bottom:0}
.row-main{display:flex;flex-direction:column;min-width:0}
.row-sub{font-size:13px;color:var(--muted)}
.row-value{color:var(--muted);text-align:right;flex-shrink:0;max-width:60%}
.row form{flex-shrink:0}
.hint{font-size:13px;color:var(--muted)}
/* chips and banners */
.chip{font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap;background:var(--quiet);color:var(--text)}
.chip.info{background:var(--info);color:var(--info-text)}.chip.ok{background:var(--ok-bg);color:var(--ok)}
.chip.warn{background:var(--warn-bg);color:var(--warn)}.chip.err{background:var(--err-bg);color:var(--err)}
.banner{border-radius:12px;padding:12px 14px;font-size:15px;background:var(--info);color:var(--info-text)}
.banner.bad{background:var(--err-bg);color:var(--err)}
.alert{border-radius:10px;padding:10px 12px;font-size:14px;background:var(--warn-bg);color:var(--warn)}
.alert ul{margin:6px 0 0;padding-left:18px}
/* content */
pre{background:var(--code);padding:10px 12px;border-radius:10px;white-space:pre-wrap;word-break:break-word;max-height:24rem;overflow:auto;font:14px/1.45 ui-monospace,Menlo,monospace;margin:0}
dl{display:grid;grid-template-columns:minmax(4.5rem,max-content) minmax(0,1fr);gap:6px 14px;margin:0;font-size:15px}
dt{color:var(--muted)} dd{margin:0;word-break:break-word}
.meta{font-size:13px;color:var(--muted)}
/* buttons */
.btn{font:inherit;font-size:15px;font-weight:500;min-height:44px;padding:0 18px;border-radius:10px;border:0;cursor:pointer;
background:var(--quiet);color:var(--text);text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px}
.btn.primary{background:var(--accent);color:#fff;font-weight:600}
.btn.danger{color:var(--err)}
.btn.text{background:none;color:var(--accent-text);padding:0 4px;min-width:5.5rem;justify-content:flex-end}
.btn.text.danger{color:var(--err)}
.btn[disabled]{opacity:.5;cursor:default}
.btn svg{width:20px;height:20px}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
/* forms */
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:14px;font-weight:600}
.field input{width:100%;min-height:46px;padding:0 12px;font:inherit;font-size:16px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--text)}
.check{display:flex;gap:10px;align-items:flex-start;font-size:14px}
.check input{width:20px;height:20px;margin:1px 0 0;flex-shrink:0}
/* segmented filter */
.seg{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;background:var(--quiet);border-radius:9px;padding:2px;gap:2px}
.seg a{min-height:34px;display:flex;align-items:center;justify-content:center;border-radius:7px;font-size:13px;color:var(--text);text-decoration:none}
.seg a[aria-current=page]{background:var(--card);font-weight:600;box-shadow:0 1px 3px var(--shadow)}
/* activity */
ol.log{list-style:none;margin:0;padding:0}
ol.log li{display:grid;grid-template-columns:3rem minmax(0,1fr);gap:10px;padding:10px 0;border-bottom:1px solid var(--quiet);font-size:15px}
ol.log li:last-child{border-bottom:0}
ol.log time{font:12px/1.9 ui-monospace,Menlo,monospace;color:var(--muted);font-variant-numeric:tabular-nums}
/* tab bar (phones) */
.tabs{position:fixed;left:0;right:0;bottom:0;z-index:2;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;background:var(--bar);
border-top:1px solid var(--line);padding:4px 4px calc(6px + env(safe-area-inset-bottom,0px))}
.tabs a{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:50px;text-decoration:none;color:var(--muted);font-size:11px;font-weight:500}
.tabs a[aria-current=page]{color:var(--accent);font-weight:600}
.tabs svg{width:24px;height:24px}
.ico{position:relative;display:inline-flex}
.count{position:absolute;top:-4px;right:-11px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--badge);color:#fff;font-size:11px;font-weight:700;line-height:18px;text-align:center}
/* sidebar (wide screens) */
.side{display:none}
/* sign-in */
.auth{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px}
.auth .card{width:100%;max-width:25rem}
.auth .card-head{padding:18px 18px 0}
.auth .card-body{padding:14px 18px 18px;gap:14px}
.auth .card-foot{justify-content:flex-start}
.auth h1{font-size:24px;font-weight:700}
.or{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}
.or::before,.or::after{content:"";flex:1;height:1px;background:var(--line)}
.full{width:100%}
@media (min-width:640px){.card-foot .btn{flex:0 0 auto;min-width:9rem}.auth .card-foot .btn{flex:1 1 auto}}
@media (min-width:860px){
.topbar,.tabs{display:none}
.side{display:flex;flex-direction:column;gap:2px;width:260px;flex-shrink:0;padding:20px 12px;border-right:1px solid var(--line);background:var(--bar);position:sticky;top:0;height:100vh}
.side .brand{padding:0 8px 18px;flex:0 0 auto}
.side nav{display:flex;flex-direction:column;gap:2px}
.side nav a{display:flex;align-items:center;gap:10px;min-height:40px;padding:0 10px;border-radius:8px;color:var(--text);text-decoration:none}
.side nav a[aria-current=page]{background:var(--info);color:var(--info-text);font-weight:600}
.side svg{width:20px;height:20px}
.side .num{margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--badge);color:#fff;font-size:12px;font-weight:700;line-height:20px;text-align:center}
.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding:12px 8px 0;border-top:1px solid var(--line)}
.side-foot .chip{align-self:flex-start}
main{padding:0 40px 40px}
.page-head{padding-top:32px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
_ICONS = {
    "overview": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"></rect>',
    "approvals": '<path d="M4 13l2.2-8h11.6L20 13v6H4z"></path><path d="M4 13h5l1 2h4l1-2h5"></path>',
    "activity": '<circle cx="12" cy="12" r="8.5"></circle><path d="M12 7.5V12l3 2"></path>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2"></circle><circle cx="9" cy="17" r="2"></circle>',
    "security": '<path d="M12 3l8 3v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6z"></path>',
    "key": '<circle cx="8" cy="15" r="4"></circle><path d="M11 12l9-9M17 6l3 3M14 9l2 2"></path>',
}


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


# -- components (each returns HTML; arguments named *_html are already escaped) ----------------------------------------
def card(title: str = "", body_html: str = "", foot_html: str = "", chip_html: str = "", form: str = "", hidden_html: str = "") -> str:
    """One card. form='/dashboard/x' wraps the whole card in a POST form, so its fields and its foot buttons belong together."""
    head = f'<div class="card-head"><h2>{_esc(title)}</h2>{chip_html}</div>' if title else ""
    inner = (head + (f'<div class="card-body">{body_html}</div>' if body_html else "")
             + (f'<div class="card-foot">{foot_html}</div>' if foot_html else ""))
    if form:
        return f'<form class="card" method="post" action="{_esc(form)}">{hidden_html}{inner}</form>'
    return f'<section class="card">{inner}</section>'


def chip(text: str, tone: str = "") -> str:
    return f'<span class="chip {tone}">{_esc(text)}</span>'


def row(label: str, value_html: str = "", sub: str = "") -> str:
    main = f'<span class="row-main"><span>{_esc(label)}</span>' + (f'<span class="row-sub">{_esc(sub)}</span>' if sub else "") + "</span>"
    return f'<div class="row">{main}{value_html}</div>'


def value(text: str) -> str:
    return f'<span class="row-value">{_esc(text)}</span>'


def button(label: str, kind: str = "", **attrs: str) -> str:
    extra = "".join(f' {k.replace("_", "-")}="{_esc(v)}"' for k, v in attrs.items())
    if "type" not in attrs:
        extra = ' type="submit"' + extra
    return f'<button class="btn {kind}"{extra}>{label}</button>'


def link_button(label: str, href: str, kind: str = "") -> str:
    return f'<a class="btn {kind}" href="{_esc(href)}">{_esc(label)}</a>'


def field(fid: str, label: str, input_attrs: str, hint: str = "") -> str:
    return (f'<div class="field"><label for="{fid}">{_esc(label)}</label><input id="{fid}" {input_attrs}>'
            + (f'<p class="hint">{_esc(hint)}</p>' if hint else "") + "</div>")


def hint(text: str) -> str:
    return f'<p class="hint">{_esc(text)}</p>'


class Dashboard:
    def __init__(self, mcp: Any, provider: Any, s: Settings, audit: AuditLog, sources: list[ApprovalSource],
                 exit_fn: Callable[[], None] | None = None):
        self.mcp, self.provider, self.s, self.audit = mcp, provider, s, audit
        self.sources = {src.kind: src for src in sources}
        self.sessions = Sessions(s.data_dir, s.dashboard_session_minutes)
        self.passkeys = P.PasskeyStore(s.data_dir, s.public_url)
        self.exit_fn = exit_fn or (lambda: os._exit(0))
        self.secure = s.public_url.startswith("https://")
        self.cookie = "__Host-icloud-dashboard" if self.secure else "icloud-dashboard"
        self.origin = "{0.scheme}://{0.netloc}".format(urlsplit(s.public_url))
        self.host = urlsplit(s.public_url).netloc
        self.tz = self._tz()
        self.last_health: dict[str, Any] | None = None
        self.pause_file = os.path.join(s.data_dir, "paused")
        self.extra_nav: list[tuple[str, str, str]] = []        # (key, label, path) added by other parts (settings)
        self.has_icon = (_STATIC / "icon-32.png").is_file()

    def _tz(self):
        try:
            return ZoneInfo(self.s.default_timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    # -- helpers ---------------------------------------------------------------------------
    def waiting(self) -> int:
        return sum(len(src.pending()) for src in self.sources.values())

    def paused(self) -> bool:
        return os.path.exists(self.pause_file)

    def password_login(self) -> bool:
        """The owner password signs in unless the owner turned it off, and never when that would leave no way in."""
        return self.s.owner_password_login or not self.passkeys.list()

    def session(self, request: Request) -> tuple[str, dict[str, Any] | None]:
        token = request.cookies.get(self.cookie, "")
        return token, self.sessions.get(token)

    def same_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin:
            return origin == self.origin
        ref = request.headers.get("referer")
        return bool(ref) and ref.startswith(self.origin + "/")

    @staticmethod
    def csrf_input(rec: dict[str, Any]) -> str:
        return f'<input type="hidden" name="csrf" value="{_esc(rec["csrf"])}">'

    def brand(self) -> str:
        icon = '<img src="/icon-32.png" alt="">' if self.has_icon else ""
        return (f'<div class="brand">{icon}<span class="brand-text"><span class="brand-name">iCloud connector</span>'
                f'<span class="brand-host">{_esc(self.host)}</span></span></div>')

    def state_chip(self) -> str:
        return chip("Paused", "warn") if self.paused() else chip("Running", "ok")

    def page(self, title: str, active: str, inner: str, rec: dict[str, Any], status: int = 200, flash: str = "",
             bad: bool = False, lede: str = "") -> Response:
        """A signed-in page: top bar or sidebar, the page head, a banner for the last action, then the cards."""
        n = self.waiting()
        items = [("overview", "Overview", "/dashboard"), ("approvals", "Approvals", "/dashboard/approvals"),
                 ("activity", "Activity", "/dashboard/activity"), *self.extra_nav, ("security", "Security", "/dashboard/security")]
        tabs, side = "", ""
        for key, label, href in items:
            cur = ' aria-current="page"' if key == active else ""
            icon = _SVG.format(_ICONS[key])
            badge = f'<span class="count" aria-label="{n} waiting">{n}</span>' if key == "approvals" and n else ""
            tabs += f'<a href="{href}"{cur}><span class="ico">{icon}{badge}</span><span>{label}</span></a>'
            side += f'<a href="{href}"{cur}>{icon}<span>{label}</span>' + (f'<span class="num">{n}</span>' if key == "approvals" and n else "") + "</a>"
        sidebar = (f'<aside class="side">{self.brand()}<nav aria-label="Sections">{side}</nav>'
                   f'<div class="side-foot">{self.state_chip()}<form method="post" action="/dashboard/logout">{self.csrf_input(rec)}'
                   f'{button("Sign out", "full")}</form></div></aside>')
        topbar = f'<header class="topbar">{self.brand()}{self.state_chip()}</header>'
        banner = f'<p class="banner{" bad" if bad else ""}" role="status">{_esc(flash)}</p>' if flash else ""
        head = f'<div class="page-head"><h1>{_esc(title)}</h1>' + (f'<p class="lede">{_esc(lede)}</p>' if lede else "") + "</div>"
        return self._doc(title, f'<div class="wrap">{sidebar}<div style="flex:1;min-width:0">{topbar}<main>{head}'
                                f'<div class="stack">{banner}{inner}</div></main></div></div><nav class="tabs" aria-label="Sections">{tabs}</nav>', status)

    def auth_page(self, title: str, body_html: str, foot_html: str = "", status: int = 200) -> Response:
        """Sign-in and confirm: one card in the middle of the screen, nothing outside it."""
        inner = (f'<main class="auth"><section class="card"><div class="card-head">{self.brand()}</div>'
                 f'<div class="card-body">{body_html}</div>' + (f'<div class="card-foot">{foot_html}</div>' if foot_html else "")
                 + "</section></main>")
        return self._doc(title, inner, status)

    def _doc(self, title: str, body: str, status: int) -> Response:
        doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
               f'<meta name="color-scheme" content="light dark"><title>{_esc(title)} · iCloud connector</title>'
               f'<link rel="icon" href="/favicon.ico"><style>{_CSS}</style></head><body>{body}'
               '<script src="/dashboard/passkey.js" defer></script></body></html>')
        return HTMLResponse(doc, status_code=status, headers=_HEADERS)

    def message(self, rec: dict[str, Any], title: str, text: str, status: int) -> Response:
        return self.page(title, "", card(body_html=f'<p>{_esc(text)}</p>', foot_html=link_button("Back to the overview", "/dashboard")), rec, status)

    def fmt_time(self, t: float | None, with_day: bool = False) -> str:
        if not t:
            return "never"
        d = datetime.fromtimestamp(t, self.tz)
        return d.strftime("%d %b, %H:%M") if with_day else d.strftime("%H:%M")

    def ago(self, t: float | None) -> str:
        if not t:
            return "never"
        s = max(0, time.time() - t)
        for unit, n in (("day", 86400), ("hour", 3600), ("min", 60)):
            if s >= n:
                v = int(s // n)
                return f"{v} {unit}{'s' if v != 1 and unit != 'min' else ''} ago"
        return "just now"


def register_dashboard(mcp: Any, provider: Any, s: Settings, audit: AuditLog, sources: list[ApprovalSource],
                       exit_fn: Callable[[], None] | None = None) -> Dashboard:
    d = Dashboard(mcp, provider, s, audit, sources, exit_fn)
    js = (_STATIC / "passkey.js").read_bytes()

    # -- guards --------------------------------------------------------------------------------------------------------
    def signed_in(request: Request) -> tuple[str, dict[str, Any]] | None:
        token, rec = d.session(request)
        return (token, rec) if rec is not None else None

    def to_signin(request: Request) -> Response:
        nxt = request.url.path if request.url.path.startswith("/dashboard") else "/dashboard"
        return RedirectResponse(f"/dashboard/signin?next={quote(nxt)}", status_code=303, headers=_HEADERS)

    async def checked_form(request: Request) -> tuple[str, dict[str, Any], Any] | Response:
        """A POST from a signed-in owner, from this origin, with the session's CSRF token."""
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        if not d.same_origin(request):
            return d.message(got[1], "Not allowed", "This request did not come from this page. Nothing was changed.", 403)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), got[1]["csrf"].encode()):
            return d.message(got[1], "Expired", "This form has expired. Go back, reload the page and try again.", 403)
        return got[0], got[1], form

    def recent(rec: dict[str, Any]) -> bool:
        return time.time() - rec.get("auth_at", 0) < _STEP_UP

    def set_cookie(resp: Response, token: str) -> Response:
        resp.set_cookie(d.cookie, token, max_age=_ABSOLUTE, httponly=True, secure=d.secure, samesite="strict", path="/")
        return resp

    def safe_next(value: str) -> str:
        return value if value.startswith("/dashboard") and "//" not in value and "\\" not in value else "/dashboard"

    def sign_in_body(heading: str, lede: str, nxt: str, error: str, rec: dict[str, Any] | None) -> tuple[str, str]:
        """The shared body of the sign-in and the confirm card: passkey first, then the password, then any error."""
        pk = bool(d.passkeys.list()) and P.available()
        pw = d.password_login()
        parts = [f'<div style="display:flex;flex-direction:column;gap:4px"><h1>{_esc(heading)}</h1><p class="lede">{_esc(lede)}</p></div>']
        if error:
            parts.append(f'<p class="banner bad" role="alert">{_esc(error)}</p>')
        if pk:
            parts.append(button(_SVG.format(_ICONS["key"]) + "<span>Sign in with a passkey</span>" if rec is None else
                                _SVG.format(_ICONS["key"]) + "<span>Confirm with your passkey</span>", "primary full",
                                type="button", data_passkey="signin", data_next=nxt))
        if pk and pw:
            parts.append('<div class="or">or use the owner password</div>')
        if pw:
            hidden = (d.csrf_input(rec) if rec else "") + f'<input type="hidden" name="next" value="{_esc(nxt)}">'
            parts.append(f'<form method="post" action="/dashboard/signin" style="display:flex;flex-direction:column;gap:12px">{hidden}'
                         + field("pw", "Owner password", 'type="password" name="password" autocomplete="current-password" required'
                                 + ("" if pk else " autofocus"))
                         + button("Sign in with password" if rec is None else "Confirm with password", "full" if pk else "primary full")
                         + "</form>")
        parts.append('<p class="banner bad" data-passkey-error hidden></p>')
        return "".join(parts), ""

    # -- static ----------------------------------------------------------------------------------------------------------
    @mcp.custom_route("/dashboard/passkey.js", methods=["GET"])
    async def passkey_js(_: Request) -> Response:
        return Response(js, media_type="text/javascript", headers={**_HEADERS, "Cache-Control": "public, max-age=3600"})

    # -- sign in and out -------------------------------------------------------------------------------------------------
    def signin_page(error: str = "", nxt: str = "/dashboard", status: int = 200) -> Response:
        body, _ = sign_in_body("Sign in", "Review what your agents want to send, see what they did, and manage this server.", nxt, error, None)
        foot = ('<p class="hint">' + ("" if d.passkeys.list() else "After you sign in, you can add a passkey under Security. ")
                + "Ten wrong passwords in 15 minutes lock sign-in for everyone for a while.</p>")
        return d.auth_page("Sign in", body, foot, status)

    def confirm_page(rec: dict[str, Any], back: str) -> Response:
        body, _ = sign_in_body("Confirm it’s you", "This step changes how your server is protected, so it asks you to sign in again.",
                               back, "", rec)
        return d.auth_page("Confirm", body, link_button("Cancel", back))

    @mcp.custom_route("/dashboard/signin", methods=["GET"])
    async def signin_get(request: Request) -> Response:
        if signed_in(request):
            return RedirectResponse(safe_next(request.query_params.get("next", "/dashboard")), status_code=303, headers=_HEADERS)
        return signin_page(nxt=safe_next(request.query_params.get("next", "/dashboard")))

    @mcp.custom_route("/dashboard/signin", methods=["POST"])
    async def signin_post(request: Request) -> Response:
        if not d.same_origin(request):
            return signin_page("This request did not come from this page.", status=403)
        form = await request.form()
        nxt = safe_next(str(form.get("next", "/dashboard")))
        if not d.password_login():
            return signin_page("Sign-in with the owner password is turned off. Use your passkey.", nxt, 403)
        if not provider.login_allowed():
            return signin_page("Too many failed attempts. Try again in 15 minutes.", nxt, 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), s.owner_password.encode()):
            provider.record_failure()
            log.warning("Failed owner-password attempt on the dashboard")
            audit.record("signin.password", actor="unknown", ok=False, summary="Entered a wrong owner password",
                         details={"from": request.client.host if request.client else ""})
            return signin_page("Incorrect password.", nxt, 401)
        token, rec = d.session(request)
        if rec is not None:                                  # confirming an existing session
            d.sessions.reauth(token, "password")
            audit.record("signin.confirm", actor="owner", via="password", summary="Confirmed with the owner password")
            return RedirectResponse(nxt, status_code=303, headers=_HEADERS)
        token, rec = d.sessions.create("password")
        audit.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
        return set_cookie(RedirectResponse(nxt, status_code=303, headers=_HEADERS), token)

    @mcp.custom_route("/dashboard/logout", methods=["POST"])
    async def logout(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        d.sessions.drop(got[0])
        audit.record("signin.signout", actor="owner", via=got[1].get("via", ""), summary="Signed out of the dashboard")
        resp = RedirectResponse("/dashboard/signin", status_code=303, headers=_HEADERS)
        resp.delete_cookie(d.cookie, path="/", secure=d.secure, httponly=True, samesite="strict")
        return resp

    # -- passkey JSON endpoints (passkey.js) ---------------------------------------------------------------------------
    async def json_body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @mcp.custom_route("/dashboard/passkey/options", methods=["POST"])
    async def passkey_options(request: Request) -> Response:
        if not d.same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=_HEADERS)
        purpose = str((await json_body(request)).get("purpose", ""))
        try:
            if purpose == "signin":
                if not provider.login_allowed():
                    return JSONResponse({"error": "Too many failed attempts. Try again in 15 minutes."}, status_code=429, headers=_HEADERS)
                return JSONResponse(d.passkeys.authentication_options(), headers=_HEADERS)
            if purpose == "register":
                got = signed_in(request)
                if got is None or not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), got[1]["csrf"].encode()):
                    return JSONResponse({"error": "Sign in first."}, status_code=401, headers=_HEADERS)
                if not recent(got[1]):
                    return JSONResponse({"error": "confirm", "confirm": "/dashboard/security/confirm"}, status_code=403, headers=_HEADERS)
                return JSONResponse(d.passkeys.registration_options(), headers=_HEADERS)
        except P.PasskeyError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=_HEADERS)
        return JSONResponse({"error": "unknown purpose"}, status_code=400, headers=_HEADERS)

    @mcp.custom_route("/dashboard/passkey/verify", methods=["POST"])
    async def passkey_verify(request: Request) -> Response:
        if not d.same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=_HEADERS)
        data = await json_body(request)
        purpose, ticket = str(data.get("purpose", "")), str(data.get("ticket", ""))
        credential = data.get("credential") if isinstance(data.get("credential"), dict) else {}
        nxt = safe_next(str(data.get("next", "/dashboard")))
        if purpose == "signin":
            try:
                key = await asyncio.to_thread(d.passkeys.authenticate, ticket, credential)
            except P.PasskeyError as e:
                provider.record_failure()
                audit.record("signin.passkey", actor="unknown", ok=False, summary="Tried to sign in with a passkey that did not verify")
                return JSONResponse({"error": str(e)}, status_code=401, headers=_HEADERS)
            token, rec = d.session(request)
            if rec is not None:
                d.sessions.reauth(token, f"passkey “{key.name}”")
                audit.record("signin.confirm", actor="owner", via=f"passkey “{key.name}”", summary=f"Confirmed with passkey “{key.name}”")
                return JSONResponse({"next": nxt}, headers=_HEADERS)
            token, rec = d.sessions.create(f"passkey “{key.name}”")
            audit.record("signin.passkey", actor="owner", via=f"passkey “{key.name}”", summary=f"Signed in with passkey “{key.name}”")
            return set_cookie(JSONResponse({"next": nxt}, headers=_HEADERS), token)
        if purpose == "register":
            got = signed_in(request)
            if got is None or not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), got[1]["csrf"].encode()) or not recent(got[1]):
                return JSONResponse({"error": "Sign in again first."}, status_code=401, headers=_HEADERS)
            try:
                key = await asyncio.to_thread(d.passkeys.register, ticket, credential, str(data.get("name", "")))
            except P.PasskeyError as e:
                return JSONResponse({"error": str(e)}, status_code=400, headers=_HEADERS)
            audit.record("passkey.added", actor="owner", via=got[1].get("via", ""), summary=f"Added passkey “{key.name}”")
            return JSONResponse({"next": "/dashboard/security?m=passkey_added"}, headers=_HEADERS)
        return JSONResponse({"error": "unknown purpose"}, status_code=400, headers=_HEADERS)

    # -- overview ------------------------------------------------------------------------------------------------------
    @mcp.custom_route("/dashboard", methods=["GET"])
    async def overview(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        rec = got[1]
        kinds = {}
        for src in d.sources.values():
            if c := len(await asyncio.to_thread(src.pending)):
                kinds[src.badge] = c
        n = sum(kinds.values())
        try:
            version = importlib.metadata.version("icloud-mcp-server")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        paused = d.paused()
        csrf = d.csrf_input(rec)
        cards = []
        if n:
            cards.append(card("Waiting for your approval", f'<p>{_esc(", ".join(f"{c} {k.lower()}" for k, c in kinds.items()))}. '
                              'Nothing has left the server yet.</p>', link_button("Review", "/dashboard/approvals", "primary"), chip(str(n), "info")))
        health = d.last_health
        if health is None:
            services = hint("Not checked since the server started.")
            state = ""
        else:
            services = '<div class="list">' + "".join(
                row(area.replace("_", " ").capitalize(),
                    chip(f'OK · {r.get("ms", 0) / 1000:.1f} s', "ok") if r.get("ok") else chip("Failed", "err"),
                    "" if r.get("ok") else (r.get("error") or "")[:120])
                for area, r in (health.get("areas") or {}).items()) + "</div>" + hint(f'Checked {d.ago(health.get("_at"))}.')
            state = chip("All working", "ok") if health.get("ok") else chip("Problem", "err")
        cards.append(card("Services", services, button("Check now"), state, form="/dashboard/check", hidden_html=csrf))
        apps = provider.connected_apps()
        server_rows = ('<div class="list">'
                       + row("Tools", value(f"{len(mcp._tool_manager.list_tools())} offered"))
                       + row("Connected apps", value(str(len(apps))))
                       + row("Version", value(version)) + "</div>"
                       + hint("Pausing makes every tool answer that the server is paused. Nobody is signed out."))
        cards.append(card("Server", server_rows, button("Resume tools" if paused else "Pause tools", "" if not paused else "primary"),
                          d.state_chip(), form="/dashboard/pause", hidden_html=csrf + f'<input type="hidden" name="paused" value="{"0" if paused else "1"}">'))
        if s.read_only:
            sending = "Off (read-only)"
        elif not s.allow_send:
            sending = "Drafts only"
        else:
            sending = "After approval" if s.require_approval else "Directly"
        invites = "After approval" if getattr(s, "invites_require_approval", False) else ("Allowed" if s.allow_calendar_invites else "Off")
        prot = ('<div class="list">' + row("Sending email", value(sending)) + row("Invitations to others", value(invites))
                + row("Emptying Trash", value("Allowed" if s.allow_permanent_delete else "Off"))
                + row("Time zone", value(s.default_timezone)) + "</div>")
        has_settings = any(k == "settings" for k, _, _ in d.extra_nav)
        cards.append(card("Protection", prot, link_button("All settings", "/dashboard/settings") if has_settings else ""))
        entries = audit.entries(limit=5)
        acts = "".join(f'<li><time>{d.fmt_time(e.get("t"))}</time><span>{_entry_text(e)}</span></li>' for e in entries)
        cards.append(card("Recent activity", f'<ol class="log">{acts}</ol>' if acts else hint("Nothing recorded yet."),
                          link_button("Open the activity log", "/dashboard/activity")))
        return d.page("Overview", "overview", "".join(cards), rec, flash=_flash(request))

    @mcp.custom_route("/dashboard/check", methods=["POST"])
    async def check(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        fn = getattr(mcp, "_icloud_health", None)
        if fn is not None:
            try:
                r = await asyncio.wait_for(asyncio.to_thread(fn), timeout=45)
            except asyncio.TimeoutError:
                r = {"ok": False, "areas": {"check": {"ok": False, "error": "took longer than 45 s"}}}
            d.last_health = {**r, "_at": time.time()}
        return RedirectResponse("/dashboard?m=checked", status_code=303, headers=_HEADERS)

    @mcp.custom_route("/dashboard/pause", methods=["POST"])
    async def pause(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        want = str(got[2].get("paused", "1")) == "1"
        from .admin import _write_private
        if want:
            _write_private(d.pause_file, time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        elif os.path.exists(d.pause_file):
            os.unlink(d.pause_file)
        log.warning("Owner %s the server from the dashboard", "paused" if want else "resumed")
        audit.record("server.paused" if want else "server.resumed", actor="owner", via=got[1].get("via", ""),
                     summary="Paused all tools" if want else "Resumed all tools")
        return RedirectResponse("/dashboard?m=" + ("paused" if want else "resumed"), status_code=303, headers=_HEADERS)

    # -- approvals -----------------------------------------------------------------------------------------------------
    def approvals_page(rec: dict[str, Any], flash: str = "", bad: bool = False, status: int = 200) -> Response:
        cards, total, recent_n = [], 0, 0
        for src in d.sources.values():
            items = src.pending()
            ctx = src.contexts(items)
            for q in items:
                total += 1
                recent_n += time.time() - q.created_at < 3600
                v = src.describe(q, ctx.get(q.id))
                mins = max(1, int((q.expires_at - time.time()) / 60))
                left = f"{mins // 60} h" if mins >= 120 else f"{mins} min"
                body = [f'<dl>{"".join(f"<dt>{_esc(k)}</dt><dd>{_esc(val)}</dd>" for k, val in v["rows"])}</dl>']
                if v["warnings"]:
                    body.append('<div class="alert" role="alert"><b>Check this first</b><ul>'
                                + "".join(f"<li>{_esc(w)}</li>" for w in v["warnings"]) + "</ul></div>")
                if v["text"]:
                    body.append(f'<pre>{_esc(v["text"])}</pre>')
                body += [hint(x) for x in v["notes"]]
                who = f" by {q.requested_by}" if getattr(q, "requested_by", None) else ""
                body.append(f'<p class="meta">Requested {d.ago(q.created_at)}{_esc(who)} · expires in about {left}</p>')
                if v["needs_override"]:
                    body.append('<label class="check"><input type="checkbox" name="override" value="1">'
                                '<span>I read the warning and still want this sent</span></label>')
                hidden = (d.csrf_input(rec) + f'<input type="hidden" name="kind" value="{_esc(src.kind)}">'
                          f'<input type="hidden" name="id" value="{_esc(q.id)}"><input type="hidden" name="sha" value="{_esc(q.sha256)}">')
                foot = button("Discard", name="action", value="discard") + button(_esc(src.approve_label), "primary", name="action", value="approve")
                cards.append(card(v["title"], "".join(body), foot, chip(v["badge"], "info"), form="/dashboard/approvals/act", hidden_html=hidden))
        if not total:
            cards.append(card("Nothing is waiting", hint("When an agent sends mail or invites someone, it waits here until you decide.")))
        if recent_n > 2:
            cards.insert(0, f'<p class="alert">Agents asked for {recent_n} of these in the last hour. Approve only what you asked for.</p>')
        lede = f"{total} waiting. Nothing has left the server yet; what you see is exactly what will be sent." if total else ""
        return d.page("Approvals", "approvals", "".join(cards), rec, status, flash, bad, lede)

    @mcp.custom_route("/dashboard/approvals", methods=["GET"])
    async def approvals_get(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        return await asyncio.to_thread(approvals_page, got[1])

    @mcp.custom_route("/dashboard/approvals/act", methods=["POST"])
    async def approvals_act(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        src = d.sources.get(str(form.get("kind", "")))
        action = str(form.get("action", ""))
        q = next((x for x in src.pending() if x.id == str(form.get("id", ""))), None) if src else None
        if q is None or action not in ("approve", "discard") or not hmac.compare_digest(str(form.get("sha", "")).encode(), q.sha256.encode()):
            return await asyncio.to_thread(approvals_page, rec, "That item is no longer waiting or has changed. Nothing was done.", True, 409)
        v = await asyncio.to_thread(src.describe, q, src.contexts([q]).get(q.id))
        who = {"actor": "owner", "via": rec.get("via", "")}
        if action == "discard":
            await asyncio.to_thread(src.discard, q)
            audit.record("approval.discarded", **who, summary=f"Discarded {v['badge'].lower()} “{v['title']}”", kind=src.kind)
            return await asyncio.to_thread(approvals_page, rec, "Discarded. Nothing was sent.")
        if v["needs_override"] and str(form.get("override", "")) != "1":
            return await asyncio.to_thread(approvals_page, rec, "Not sent: tick the box under the warning if you still want it sent.", True)
        try:
            note = await asyncio.to_thread(src.release, q)
        except Exception as e:  # noqa: BLE001  -- release() puts the item back; the owner can retry or discard
            log.warning("Release of %s %s failed: %s", src.kind, q.id, e)
            audit.record("approval.failed", **who, ok=False, summary=f"Could not send {v['badge'].lower()} “{v['title']}”", kind=src.kind)
            return await asyncio.to_thread(approvals_page, rec, f"Not sent (still waiting): {e}", True)
        audit.record("approval.sent", **who, summary=f"Approved {v['badge'].lower()} “{v['title']}”", kind=src.kind,
                     recipients=[val for k, val in v["rows"] if k in ("To", "Guests", "Will be delivered to")])
        return await asyncio.to_thread(approvals_page, rec, note)

    # -- activity ------------------------------------------------------------------------------------------------------
    _FILTERS = {"all": ("All", ()), "approvals": ("Approvals", ("approval.",)), "agents": ("Agents", ("tool.",)),
                "signins": ("Sign-ins", ("signin.", "passkey.", "app."))}

    @mcp.custom_route("/dashboard/activity", methods=["GET"])
    async def activity(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        which = request.query_params.get("filter", "all")
        which = which if which in _FILTERS else "all"
        entries = await asyncio.to_thread(audit.entries, 300, _FILTERS[which][1])
        chain = await asyncio.to_thread(audit.verify)
        seg = "".join(f'<a href="/dashboard/activity?filter={k}"{" aria-current=page" if k == which else ""}>{label}</a>'
                      for k, (label, _) in _FILTERS.items())
        cards = [f'<nav class="seg" aria-label="Filter">{seg}</nav>']
        if not s.audit_log:
            cards.append('<p class="alert">The activity log is turned off on this server (AUDIT_LOG=false).</p>')
        days: dict[Any, list[dict[str, Any]]] = {}
        today = datetime.now(d.tz).date()
        for e in entries:
            days.setdefault(datetime.fromtimestamp(e.get("t", 0), d.tz).date(), []).append(e)
        for day, items in days.items():
            label = "Today" if day == today else "Yesterday" if day == today - timedelta(days=1) else day.strftime("%a %d %b %Y")
            lis = "".join(f'<li><time>{d.fmt_time(e.get("t"))}</time><span>{_entry_text(e, full=True)}</span></li>' for e in items)
            cards.append(card(label, f'<ol class="log">{lis}</ol>'))
        if not entries:
            cards.append(card("Nothing recorded yet", hint("Writes by agents, your approvals and every sign-in appear here.")))
        since = d.fmt_time(chain.get("since"), with_day=True) if chain.get("since") else ""
        if chain["ok"]:
            state, text = chip("Verified", "ok"), f'{chain["entries"]} entries, none changed since they were written' + (f" (oldest kept: {since})." if since else ".")
        else:
            state, text = chip("Changed", "err"), f'The log was changed after it was written, at entry {chain.get("broken_at")}. Entries from there on cannot be trusted.'
        cards.append(card("About this log", f"<p>{_esc(text)}</p>" + hint(f"Kept for {s.audit_keep_days} days. Names and addresses only; "
                                                                          "no message bodies, passwords or tokens."),
                          link_button("Download as JSON", "/dashboard/activity.json"), state))
        return d.page("Activity", "activity", "".join(cards), got[1], lede="What changed on this server, newest first.")

    @mcp.custom_route("/dashboard/activity.json", methods=["GET"])
    async def activity_json(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        entries = await asyncio.to_thread(audit.entries, 100000)
        return Response(json.dumps(list(reversed(entries)), ensure_ascii=False, indent=1), media_type="application/json",
                        headers={**_HEADERS, "Content-Disposition": 'attachment; filename="icloud-mcp-activity.json"'})

    # -- security ------------------------------------------------------------------------------------------------------
    def row_action(action: str, rec: dict[str, Any], hidden: dict[str, str], label: str, danger: bool = False) -> str:
        fields = "".join(f'<input type="hidden" name="{k}" value="{_esc(v)}">' for k, v in hidden.items())
        return f'<form method="post" action="{action}">{d.csrf_input(rec)}{fields}{button(_esc(label), "text danger" if danger else "text")}</form>'

    def security_page(rec: dict[str, Any], flash: str = "", bad: bool = False, status: int = 200) -> Response:
        keys = d.passkeys.list()
        cards = []
        key_list = ('<div class="list">' + "".join(
            row(k.name, row_action("/dashboard/security/passkey/remove", rec, {"id": k.id}, "Remove", danger=True),
                f"Added {d.fmt_time(k.created_at, True)} · last used {d.ago(k.last_used_at)}") for k in keys) + "</div>") if keys else \
            hint("No passkey yet. A passkey signs you in with Face ID, Touch ID or a security key, and only on this address.")
        if P.available():
            body = key_list + field("pkname", "Name for a new passkey", 'type="text" maxlength="40" placeholder="For example: iPhone"') \
                + '<p class="banner bad" data-passkey-error hidden></p>'
            foot = button(_SVG.format(_ICONS["key"]) + "<span>Add a passkey</span>", "primary", type="button", data_passkey="register",
                          data_csrf=rec["csrf"], data_name_from="pkname")
        else:
            body, foot = key_list + '<p class="alert">This server cannot use passkeys: the optional webauthn package is not installed.</p>', ""
        cards.append(card("Passkeys", body, foot, chip(str(len(keys)), "info") if keys else ""))
        apps = provider.connected_apps()
        app_list = ('<div class="list">' + "".join(
            row(a["name"], row_action("/dashboard/security/apps/sign-out", rec, {"id": a["id"]}, "Sign out"),
                f'{a["host"]} · active {d.ago(a["last_used"])}' if a["host"] else f'active {d.ago(a["last_used"])}') for a in apps) + "</div>") \
            if apps else hint("No app is signed in.")
        foot = (f'<form method="post" action="/dashboard/security/apps/sign-out-all">{d.csrf_input(rec)}'
                f'{button("Sign out all apps", "danger")}</form>') if apps else ""
        cards.append(card("Connected apps", app_list + hint("A signed-out app has to connect again, and that needs you."),
                          foot, chip(str(len(apps)), "info") if apps else ""))
        pw_on = d.password_login()
        cards.append(card("Owner password",
                          '<div class="list">' + row("Sign-in with password", value("Allowed" if pw_on else "Off")) + "</div>"
                          + field("np", "New owner password", 'type="password" name="password" autocomplete="new-password" minlength="12" required',
                                  "At least 12 characters. It also approves new apps on the connect page and takes effect after the server restarts."),
                          button("Change password"), form="/dashboard/security/password", hidden_html=d.csrf_input(rec)))
        cards.append(card("This browser", '<div class="list">' + row("Signed in", value(d.ago(rec.get("created"))), rec.get("via", "")) + "</div>"
                          + hint("Removing a passkey, changing the password and signing out apps ask you to confirm when your sign-in is older than 5 minutes."),
                          button("Sign out", "danger"), form="/dashboard/logout", hidden_html=d.csrf_input(rec)))
        return d.page("Security", "security", "".join(cards), rec, status, flash, bad, "Who can sign in here, and which apps are connected.")

    @mcp.custom_route("/dashboard/security", methods=["GET"])
    async def security(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        return security_page(got[1], _flash(request))

    @mcp.custom_route("/dashboard/security/confirm", methods=["GET"])
    async def security_confirm(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        return confirm_page(got[1], "/dashboard/security")

    @mcp.custom_route("/dashboard/security/passkey/remove", methods=["POST"])
    async def passkey_remove(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        if not recent(rec):
            return confirm_page(rec, "/dashboard/security")
        keys = d.passkeys.list()
        if len(keys) == 1 and not s.owner_password_login:
            return security_page(rec, "This is your only passkey and password sign-in is off. Add another passkey first.", True, 409)
        key = d.passkeys.remove(str(form.get("id", "")))
        if key:
            audit.record("passkey.removed", actor="owner", via=rec.get("via", ""), summary=f"Removed passkey “{key.name}”")
        return security_page(rec, f"Removed “{key.name}”." if key else "That passkey was already gone.")

    @mcp.custom_route("/dashboard/security/apps/sign-out", methods=["POST"])
    async def app_sign_out(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        if not recent(rec):
            return confirm_page(rec, "/dashboard/security")
        cid = str(form.get("id", ""))
        name = next((a["name"] for a in provider.connected_apps() if a["id"] == cid), "an app")
        ok = provider.sign_out(cid) if cid else False
        if ok:
            audit.record("app.signed_out", actor="owner", via=rec.get("via", ""), summary=f"Signed out {name}")
        return security_page(rec, f"Signed out {name}." if ok else "That app was not signed in.", not ok)

    @mcp.custom_route("/dashboard/security/apps/sign-out-all", methods=["POST"])
    async def app_sign_out_all(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        if not recent(rec):
            return confirm_page(rec, "/dashboard/security")
        n = provider.sign_out_all()
        audit.record("app.signed_out_all", actor="owner", via=rec.get("via", ""), summary=f"Signed out all apps ({n})")
        return security_page(rec, f"Signed out {n} app{'s' if n != 1 else ''}.")

    @mcp.custom_route("/dashboard/security/password", methods=["POST"])
    async def change_password(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        if not recent(rec):
            return confirm_page(rec, "/dashboard/security")
        value_ = str(form.get("password", ""))
        if problem := owner_password_problem(value_, s.bridge_token):
            return security_page(rec, problem, True, 400)
        from .admin import save_override
        save_override(s.data_dir, "owner_password", value_)
        log.warning("Owner password changed from the dashboard; it applies after a restart")
        audit.record("owner_password.changed", actor="owner", via=rec.get("via", ""), summary="Changed the owner password")
        return security_page(rec, "Saved. The new password applies after the server restarts.")

    return d


_MESSAGES = {"checked": "Checked.", "paused": "Paused. Tools now answer that the server is paused.", "resumed": "Resumed.",
             "passkey_added": "Passkey added. You can now sign in with it."}


def _flash(request: Request) -> str:
    return _MESSAGES.get(request.query_params.get("m", ""), "")


def _entry_text(e: dict[str, Any], full: bool = False) -> str:
    """'<b>You</b> signed in with a passkey', '<b>Claude</b> moved messages (14) to Archive'. Summaries start with a verb."""
    actor = e.get("actor") or "?"
    who = "You" if actor == "owner" else ("Someone" if actor == "unknown" else actor)
    text = str(e.get("summary") or e.get("action", ""))
    out = f"<b>{_esc(who)}</b> {_esc(text[:1].lower() + text[1:])}"
    if not e.get("ok", True):
        out = f'<span style="color:var(--warn)">{out}</span>'
    if full and (e.get("details") or {}).get("error"):
        out += f'<br><span class="meta">{_esc(e["details"]["error"])}</span>'
    return out
