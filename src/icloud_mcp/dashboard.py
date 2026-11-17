"""The owner's dashboard: one place in the browser for approvals, activity, connected apps, passkeys and the server's state.

It replaces typing the owner password on every visit to /outbox with a signed-in session. Sign-in is a passkey (WebAuthn,
see passkeys.py) or the owner password; the owner can turn the password off once a passkey exists. Steps that weaken
protection (removing a passkey, changing the password, signing apps out) ask again when the last sign-in is more than five
minutes old.

Sessions: a random token in a cookie (`HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefix on https), stored only as a
SHA-256 hash in DATA_DIR/sessions.json (mode 600), so a restart keeps you signed in and a copy of the file signs nobody in.
They end after DASHBOARD_SESSION_MINUTES without use (30) and after 12 hours in any case. Every form carries a per-session
CSRF token and every POST must come from this server's own origin.

Pages are rendered on the server and work without scripts. Three scripts are served from here (CSP `script-src 'self'`):
/dashboard/passkey.js calls the browser's passkey API; /dashboard/dashboard.js keeps the pages current (it asks
/dashboard/state.json what is waiting and, on Overview and Approvals, loads the page's own address again when that
changes), sends and discards without reloading the page (a discard can be undone for 5 seconds before it is sent to the
server), and registers a service worker so the dashboard can be installed as an app; /dashboard/install-hint.js shows
the Overview's tip on how to install it. The worker caches nothing: every page comes from the server.
"""
from __future__ import annotations

import asyncio
import base64
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
    "Content-Security-Policy": ("default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                "manifest-src 'self'; worker-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
}
_LIVE = "x-live"               # header of the page's own background refresh; it does not count as using the dashboard


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

    def get(self, token: str, touch: bool = True) -> dict[str, Any] | None:
        """touch=False looks a session up without counting it as use, so a page left open does not stay signed in."""
        if not token:
            return None
        with self._lock:
            self._prune()
            rec = self._items.get(_h(token))
            if rec is None:
                return None
            if touch and time.time() - rec["last"] > 60:          # write at most once a minute while browsing
                rec["last"] = time.time()
                self._save()
            return rec

    def reauth(self, token: str, via: str) -> str | None:
        """Confirming who is there issues a new session token; the old one stops working. The CSRF value stays, because a
        change held for confirmation (settings.py) is filed under it."""
        with self._lock:
            rec = self._items.pop(_h(token), None)
            if rec is None:
                return None
            rec["auth_at"], rec["via"] = time.time(), via
            fresh = secrets.token_urlsafe(32)
            self._items[_h(fresh)] = rec
            self._save()
            return fresh

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

    def contexts(self, items: list[Any], prune: bool = True) -> dict[str, Any]:
        """prune=True only with the whole queue: it forgets what is kept for requests not in items."""
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

    def contexts(self, items, prune=True):
        return self.mail.approval_contexts(items, prune=prune) if items else {}

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


_AGENT_HINT = "Run icloud_check_health to see which service is failing."


def for_owner(message: str) -> str:
    """Service errors are written for agents. On the dashboard the owner has a button for the same check."""
    return message.replace(_AGENT_HINT, "Check now on the Overview shows which service is failing.")


def event_when(start: str | None, end: str | None, tz_name: str) -> str:
    """'Fri 20 Nov 2026, 19:00–21:00' in the server's time zone, from the ISO times stored with a held change. All-day events
    show the date only; anything unparsable is shown as stored."""
    def parse(v: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(v) if v else None
        except ValueError:
            return None
    a, b = parse(start), parse(end)
    if a is None:
        return " – ".join(x for x in (start, end) if x)
    if len(start or "") == 10:                                   # an all-day event: a date, no time
        last = b - timedelta(days=1) if b and len(end or "") == 10 else None   # DTEND of an all-day event is exclusive
        day = a.strftime("%a %d %b %Y")
        return day if last is None or last <= a else f"{day} – {last.strftime('%a %d %b %Y')}"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = None
    if tz is not None and a.tzinfo is not None:
        a, b = a.astimezone(tz), (b.astimezone(tz) if b and b.tzinfo else b)
    out = a.strftime("%a %d %b %Y, %H:%M")
    if b is None:
        return out
    return out + ("–" + b.strftime("%H:%M") if b.date() == a.date() else " – " + b.strftime("%a %d %b %Y, %H:%M"))


class CalendarApprovals(ApprovalSource):
    """Calendar changes that make iCloud email other people (ALLOW_CALENDAR_INVITES=approve)."""
    kind, badge = "calendar", "Calendar"
    _LABELS = {"create": ("Invitation", "Add and invite"), "update": ("Event update", "Save and notify"),
               "delete": ("Cancellation", "Delete and notify"), "cancel": ("Cancellation", "Cancel and notify"),
               "reply": ("Reply", "Send answer")}

    def __init__(self, cal: Any):
        self.cal = cal

    def pending(self):
        return self.cal.outbox.pending()

    def describe(self, q, context=None):
        m = q.followup or {}
        badge, _ = self._LABELS.get(m.get("op"), ("Calendar", "Approve"))
        when = event_when(m.get("start"), m.get("end"), self.cal.s.default_timezone)
        rows = [("When", when), ("Calendar", m.get("calendar") or ""), ("Where", m.get("location") or "")]
        if m.get("op") == "reply":
            rows.append(("To", m.get("organizer") or ""))
        else:
            rows.append(("Guests", ", ".join(q.recipients)))
        effect = {"create": "Approving adds the event to your calendar, and iCloud emails an invitation to",
                  "update": "Approving saves the change, and iCloud emails an update to",
                  "delete": "Approving deletes the event, and iCloud emails a cancellation to",
                  "cancel": "Approving cancels this occurrence, and iCloud emails a cancellation to",
                  "reply": "Approving sends your answer to"}.get(m.get("op"), "Approving emails")
        who = m.get("organizer") if m.get("op") == "reply" else f"{len(q.recipients)} people"
        notes = [f"{effect} {who}.",
                 "Nothing is in your calendar yet." if m.get("op") == "create" else
                 "If the event changes in your calendar before you approve, nothing is written."]
        return {"title": m.get("summary") or "(no title)", "badge": badge, "rows": [(k, v) for k, v in rows if v],
                "text": m.get("description") or "", "notes": notes, "warnings": [], "needs_override": False}

    def approve_label_for(self, q) -> str:
        return self._LABELS.get((q.followup or {}).get("op"), ("", "Approve"))[1]

    def release(self, q):
        claimed = self.cal.outbox.claim(q.id)
        if claimed is None:
            raise RuntimeError("It is no longer waiting.")
        try:
            return self.cal.release(claimed)
        except Exception:
            self.cal.outbox.restore(claimed)
            raise

    def discard(self, q):
        self.cal.outbox.claim(q.id)


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
#   top bar and tab bar (phones), or from 860 px a tinted frame: the sidebar and a bar above the page with this server's
#   address, its state and Sign out, around one white sheet that holds the page
#   page head: title, one line of context (the finding that matters most) and at most the page's one primary action
#   cards: a head (title, at most one status chip, never a button), a body (rows, text, fields), a foot (all of the
#   card's buttons, right-aligned, the main action last; full width on phones)
#   row actions (remove a passkey, sign out an app) sit at the right end of their row, all the same size
# ---------------------------------------------------------------------------------------------------------------------
_CSS = (_STATIC / "dashboard.css").read_text()
_ASSETS = {     # served under /dashboard/ to anyone (the manifest and the worker are fetched without the session cookie)
    "dashboard.css": "text/css", "dashboard.js": "text/javascript", "install-hint.js": "text/javascript",
    "manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript",
}

# The icons travel in the page and in the manifest, not as requests of their own. iOS fetches /apple-touch-icon.png and
# browsers fetch the favicon and the manifest's icons without the session cookie, so behind a sign-in proxy (Cloudflare
# Access, an SSO gateway, a Tailscale serve) they are answered with the proxy's login page and the phone falls back to a
# grey letter. Costs ~55 KB a page (the manifest is cached by its version); the routes stay for everyone else. Replacing
# the files in static/ is still all it takes to use your own artwork.
def _data_uri(name: str, ctype: str, fallback: str) -> str:
    f = _STATIC / name
    return f"data:{ctype};base64,{base64.b64encode(f.read_bytes()).decode()}" if f.is_file() else fallback


_APPLE_ICON = _data_uri("icon-180.png", "image/png", "/apple-touch-icon.png")
_FAVICON = _data_uri("favicon.ico", "image/x-icon", "/favicon.ico")


def _manifest() -> bytes:
    m = json.loads((_STATIC / "manifest.webmanifest").read_text())
    for icon in m.get("icons", []):
        src = icon.get("src", "")
        if src.startswith("/") and "/" not in src[1:]:          # one of ours, e.g. /icon-192.png
            icon["src"] = _data_uri(src[1:], icon.get("type") or "image/png", src)
    return json.dumps(m, indent=2).encode()


_MANIFEST = _manifest()
_ASSET_VERSION = hashlib.sha256(b"".join(_MANIFEST if name == "manifest.webmanifest" else (_STATIC / name).read_bytes()
                                         for name in sorted(_ASSETS))).hexdigest()[:10]

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
def card(title: str = "", body_html: str = "", foot_html: str = "", chip_html: str = "", form: str = "", hidden_html: str = "",
         link: tuple[str, str] | None = None, attrs: dict[str, str] | None = None) -> str:
    """One card. form='/dashboard/x' wraps the whole card in a POST form, so its fields and its foot buttons belong together.
    Buttons act and sit in the foot; link=(label, href) navigates and sits at the right of the head."""
    aside = chip_html + (f'<a class="head-link" href="{_esc(link[1])}">{_esc(link[0])} <span aria-hidden="true">›</span></a>' if link else "")
    head = f'<div class="card-head"><h2>{_esc(title)}</h2>{aside}</div>' if title else ""
    inner = (head + (f'<div class="card-body">{body_html}</div>' if body_html else "")
             + (f'<div class="card-foot">{foot_html}</div>' if foot_html else ""))
    if form:
        extra = "".join(f' {k}="{_esc(v)}"' for k, v in (attrs or {}).items())
        return f'<form class="card"{extra} method="post" action="{_esc(form)}">{hidden_html}{inner}</form>'
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
        self.flush_failure_lines: Callable[..., None] = lambda now=None: None   # set by register_dashboard
        self.has_icon = (_STATIC / "icon-32.png").is_file()

    def _tz(self):
        try:
            return ZoneInfo(self.s.default_timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    # -- helpers ---------------------------------------------------------------------------
    def waiting(self) -> int:
        return sum(len(src.pending()) for src in self.sources.values())

    def fingerprint(self) -> tuple[int, str]:
        """How many requests wait, and a value that changes when any of them arrives, leaves or changes (state.json)."""
        keys = [f"{src.kind}:{q.id}:{q.sha256}" for src in self.sources.values() for q in src.pending()]
        return len(keys), hashlib.sha256("\n".join(sorted(keys) + [str(self.paused())]).encode()).hexdigest()[:16]

    def paused(self) -> bool:
        return os.path.exists(self.pause_file)

    def password_login(self) -> bool:
        """The owner password signs in unless the owner turned it off, and never when that would leave no way in."""
        return self.s.owner_password_login or not self.passkeys.list()

    def session(self, request: Request) -> tuple[str, dict[str, Any] | None]:
        token = request.cookies.get(self.cookie, "")
        return token, self.sessions.get(token, touch=request.headers.get(_LIVE) != "1")

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

    def deskbar(self, rec: dict[str, Any]) -> str:
        """The bar above the sheet on wide screens: this server, whether its tools answer, and who is signed in."""
        state = '<span><span class="state-dot paused"></span>Paused</span>' if self.paused() else '<span><span class="state-dot"></span>Running</span>'
        state += '<span class="live" data-live-status hidden></span>'          # dashboard.js: “Live · updated just now”
        via = str(rec.get("via", ""))
        via = "the owner password" if via == "password" else via         # otherwise: passkey “name”, Cloudflare Access (who)
        who = f'<span class="who">Signed in with {_esc(via)}</span>' if via else '<span class="who"></span>'
        return (f'<header class="deskbar"><span class="host">{_esc(self.host)}</span>{state}{who}'
                f'<form method="post" action="/dashboard/logout">{self.csrf_input(rec)}{button("Sign out", "text")}</form></header>')

    def page(self, title: str, active: str, inner: str, rec: dict[str, Any], status: int = 200, flash: str = "",
             bad: bool = False, lede: str = "", wide: bool = False, actions: str = "", lede_html: str = "", live: str = "") -> Response:
        """A signed-in page: top bar or the frame, the page head (title, one line of context, the page's one primary
        action), a banner for the last action, then the cards. lede_html is an already escaped lede. live is the page's own
        address: dashboard.js loads it again and swaps in the content when what is waiting changes (also after a form post,
        whose address must not be requested again). Other pages only get their counts updated."""
        n, v = self.fingerprint()
        items = [("overview", "Overview", "/dashboard"), ("approvals", "Approvals", "/dashboard/approvals"),
                 ("activity", "Activity", "/dashboard/activity"), *self.extra_nav, ("security", "Security", "/dashboard/security")]
        tabs, side = "", ""
        for key, label, href in items:
            cur = ' aria-current="page"' if key == active else ""
            icon = _SVG.format(_ICONS[key])
            badge = f'<span class="count" aria-label="{n} waiting">{n}</span>' if key == "approvals" and n else ""
            tabs += f'<a href="{href}"{cur}><span class="ico">{icon}{badge}</span><span>{label}</span></a>'
            side += f'<a href="{href}"{cur}>{icon}<span>{label}</span>' + (f'<span class="num">{n}</span>' if key == "approvals" and n else "") + "</a>"
        sidebar = f'<aside class="side">{self.brand()}<nav aria-label="Sections">{side}</nav></aside>'
        topbar = f'<header class="topbar">{self.brand()}{self.state_chip()}</header>'
        banner = f'<p class="banner{" bad" if bad else ""}" role="status">{_esc(flash)}</p>' if flash else ""
        lede_html = lede_html or (_esc(lede) if lede else "")
        head = (f'<div class="page-head"><div class="page-title"><h1>{_esc(title)}</h1>' + (f'<p class="lede">{lede_html}</p>' if lede_html else "") + "</div>"
                + (f'<div class="page-actions">{actions}</div>' if actions else "") + "</div>")
        main = "<main" + (' class="wide"' if wide else "") + (f' data-live="{_esc(live)}"' if live else "") + ">"
        return self._doc(f"({n}) {title}" if n else title,
                         f'<div class="wrap" data-v="{v}">{sidebar}<div class="frame">{topbar}{self.deskbar(rec)}<div class="sheet">{main}{head}'
                         f'<div class="stack">{banner}{inner}</div></main></div></div></div><nav class="tabs" aria-label="Sections">{tabs}</nav>',
                         status, app=True)

    def auth_page(self, title: str, body_html: str, foot_html: str = "", status: int = 200) -> Response:
        """Sign-in and confirm: one card in the middle of the screen, nothing outside it."""
        inner = (f'<main class="auth"><section class="card"><div class="card-head">{self.brand()}</div>'
                 f'<div class="card-body">{body_html}</div>' + (f'<div class="card-foot">{foot_html}</div>' if foot_html else "")
                 + "</section></main>")
        return self._doc(title, inner, status)

    def _doc(self, title: str, body: str, status: int, app: bool = False) -> Response:
        """app=True (signed-in pages) adds the script for live updates, actions without reloading and the install tip."""
        v = _ASSET_VERSION
        scripts = '<script src="/dashboard/passkey.js" defer></script>'
        if app:
            scripts += f'<script src="/dashboard/install-hint.js?v={v}" defer></script><script src="/dashboard/dashboard.js?v={v}" defer></script>'
        doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
               f'<meta name="color-scheme" content="light dark"><meta name="theme-color" content="#eef2f6" media="(prefers-color-scheme: light)">'
               f'<meta name="theme-color" content="#0b0d10" media="(prefers-color-scheme: dark)"><title>{_esc(title)} · iCloud connector</title>'
               f'<link rel="icon" href="{_FAVICON}"><link rel="apple-touch-icon" href="{_APPLE_ICON}">'
               # use-credentials: the manifest request carries the proxy's cookie too, so a sign-in proxy lets it through
               f'<link rel="manifest" href="/dashboard/manifest.webmanifest?v={v}" crossorigin="use-credentials">'
               f'<link rel="stylesheet" href="/dashboard/dashboard.css?v={v}">'
               f'</head><body>{body}{scripts}</body></html>')
        return HTMLResponse(doc, status_code=status, headers=_HEADERS)

    def message(self, rec: dict[str, Any], title: str, text: str, status: int) -> Response:
        return self.page(title, "", card(body_html=f'<p>{_esc(text)}</p>', foot_html=link_button("Back to the overview", "/dashboard")), rec, status)

    @staticmethod
    def time_left(q: Any) -> str:
        mins = max(1, round((q.expires_at - time.time()) / 60))
        if mins >= 90:
            return f"{round(mins / 60)} hours"
        if mins >= 60:
            return "1 hour"
        return "1 minute" if mins == 1 else f"{mins} minutes"

    def ttl(self, q: Any, detail: bool = False) -> str:
        """Time left before a waiting request is dropped, on a track as long as its whole waiting time."""
        span = max(1.0, q.expires_at - q.created_at)
        pct = max(1, min(100, round(100 * (q.expires_at - time.time()) / span)))
        ends = (f'<span>Requested {self.fmt_time(q.created_at)}</span><span>Expires in <b>{self.time_left(q)}</b>, on '
                f'{self.fmt_time(q.expires_at, with_day=True)}</span>') if detail else f'<span>Expires in</span><b>{self.time_left(q)}</b>'
        return (f'<div class="ttl"><div class="ttl-ends">{ends}</div>'
                f'<div class="track" role="img" aria-label="{pct}% of the waiting time left"><i style="width:{pct}%"></i></div></div>')

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
    host = (urlsplit(s.public_url).hostname or "").lower()
    if not d.secure and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".localhost"):
        log.warning("MCP_PUBLIC_URL is plain http on %s: the dashboard cookie cannot be Secure or __Host-. Use https.", host)
    failures: dict[str, list[float]] = {}               # action -> [window start, recorded, not recorded]
    flashes: dict[str, str] = {}      # one-shot result of an approval, keyed by session, so the result page is a plain GET
    window = 600

    def flush_failure_lines(now: float | None = None) -> None:
        """Close every window that has ended: one line counts what was not recorded, also when the flood has stopped."""
        now = time.time() if now is None else now
        for action, (start, _written, dropped) in list(failures.items()):
            if now - start >= window:
                if dropped:
                    audit.record(action, actor="unknown", ok=False,
                                 summary=f"{int(dropped)} more like this in {window // 60} minutes were not recorded")
                del failures[action]

    def record_failure_line(action: str, summary: str, **details: Any) -> None:
        """Failed sign-ins from anyone: at most 5 lines per kind and 10 minutes, then one line that counts the rest."""
        now = time.time()
        flush_failure_lines(now)
        start, written, dropped = failures.get(action, [now, 0, 0])
        if written < 5:
            audit.record(action, actor="unknown", ok=False, summary=summary, **details)
            written += 1
        else:
            if not dropped:                                  # the first one held back: close the window on time
                try:
                    asyncio.get_running_loop().call_later(max(0.0, start + window - now) + 1, flush_failure_lines)
                except RuntimeError:                         # no event loop (called outside a request): the next call flushes
                    pass
            dropped += 1
        failures[action] = [start, written, dropped]

    d.flush_failure_lines = flush_failure_lines

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

    def _asset_route(name: str, ctype: str) -> None:
        body = _MANIFEST if name == "manifest.webmanifest" else (_STATIC / name).read_bytes()
        # Pages link the others with ?v=<content hash>, so they can be kept a day; the worker is always checked for changes.
        headers = {**_HEADERS, "Cache-Control": "no-cache" if name == "sw.js" else "public, max-age=86400"}
        if name == "sw.js":
            headers["Service-Worker-Allowed"] = "/dashboard"        # its scope includes /dashboard itself, not only /dashboard/…

        @mcp.custom_route(f"/dashboard/{name}", methods=["GET"])
        async def serve_asset(_: Request) -> Response:
            return Response(body, media_type=ctype, headers=headers)

    for _name, _ctype in _ASSETS.items():
        _asset_route(_name, _ctype)

    @mcp.custom_route("/dashboard/state.json", methods=["GET"])
    async def live_state(request: Request) -> Response:
        """What the page asks every few seconds: how many requests wait, and a fingerprint that changes when any of them
        arrives, leaves or changes. Asking does not keep the session alive (dashboard.js sends the x-live header)."""
        if signed_in(request) is None:
            return JSONResponse({"error": "signed out"}, status_code=401, headers=_HEADERS)
        n, fingerprint = await asyncio.to_thread(d.fingerprint)
        return JSONResponse({"waiting": n, "v": fingerprint}, headers=_HEADERS)

    # -- sign in and out -------------------------------------------------------------------------------------------------
    def signin_page(error: str = "", nxt: str = "/dashboard", status: int = 200) -> Response:
        body, _ = sign_in_body("Sign in", "Review what your agents want to send, see what they did, and manage this server.", nxt, error, None)
        foot = ('<p class="hint">' + ("" if d.passkeys.list() else "After you sign in, you can add a passkey under Security. ")
                + "Ten wrong passwords in 15 minutes lock password sign-in for everyone for a while.</p>")
        return d.auth_page("Sign in", body, foot, status)

    def confirm_page(rec: dict[str, Any], back: str,
                     lede: str = "This step changes how your server is protected, so it asks you to sign in again.") -> Response:
        body, _ = sign_in_body("Confirm it’s you", lede, back, "", rec)
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
            return signin_page("Too many wrong passwords. Try again in 15 minutes" + (", or use your passkey." if d.passkeys.list() else "."),
                               nxt, 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), s.owner_password.encode()):
            provider.record_failure()
            log.warning("Failed owner-password attempt on the dashboard")
            record_failure_line("signin.password", "Entered a wrong owner password",
                                details={"from": request.client.host if request.client else ""})
            return signin_page("Incorrect password.", nxt, 401)
        token, rec = d.session(request)
        if rec is not None:                                  # confirming an existing session
            if not hmac.compare_digest(str(form.get("csrf", "")).encode(), rec["csrf"].encode()):
                return signin_page("This form has expired. Reload the page and try again.", nxt, 403)
            fresh = d.sessions.reauth(token, "password")
            audit.record("signin.confirm", actor="owner", via="password", summary="Confirmed with the owner password")
            resp = RedirectResponse(nxt, status_code=303, headers=_HEADERS)
            return set_cookie(resp, fresh) if fresh else resp
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
                # Not behind the password lock: a passkey cannot be guessed, and ten wrong passwords from anyone would lock the owner out
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
                # not counted towards the password lockout: junk passkey requests must not lock the owner's password out
                record_failure_line("signin.passkey", "Tried to sign in with a passkey that did not verify")
                return JSONResponse({"error": str(e)}, status_code=401, headers=_HEADERS)
            token, rec = d.session(request)
            if rec is not None:
                fresh = d.sessions.reauth(token, f"passkey “{key.name}”")
                audit.record("signin.confirm", actor="owner", via=f"passkey “{key.name}”", summary=f"Confirmed with passkey “{key.name}”")
                resp = JSONResponse({"next": nxt}, headers=_HEADERS)
                return set_cookie(resp, fresh) if fresh else resp
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
        queued = []                     # (source, request), oldest deadline first; only the five shown are described
        for src in d.sources.values():
            queued += [(src, q) for q in await asyncio.to_thread(src.pending)]
        queued.sort(key=lambda w: w[1].expires_at)
        n = len(queued)
        waiting = []                    # (source, request, description)
        for src in d.sources.values():
            shown = [q for s2, q in queued[:5] if s2 is src]
            ctx = await asyncio.to_thread(src.contexts, shown, False) if shown else {}
            waiting += [(src, q, await asyncio.to_thread(src.describe, q, ctx.get(q.id))) for q in shown]
        waiting.sort(key=lambda w: w[1].expires_at)
        try:
            version = importlib.metadata.version("icloud-mcp-server")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        paused = d.paused()
        csrf = d.csrf_input(rec)
        apps = provider.connected_apps()
        today = datetime.now(d.tz).date()
        done_today = sum(1 for e in audit.entries(limit=300) if datetime.fromtimestamp(e.get("t", 0), d.tz).date() == today)
        if n:
            lede = (f'<b>{n} {"request is" if n == 1 else "requests are"} waiting for you.</b> Nothing has left the server; '
                    f'the {"oldest" if n > 1 else "request"} is dropped in {d.time_left(waiting[0][1])}.')
        else:
            lede = "Nothing is waiting for you."
        kpis = ('<div class="kpis">' + f'<div class="lead"><span>Waiting for you</span><b>{n}</b></div>'
                + f'<div><span>Recorded today</span><b>{done_today}</b></div>'
                + f'<div><span>Connected apps</span><b>{len(apps)}</b></div>'
                + f'<div><span>Tools offered</span><b>{len(mcp._tool_manager.list_tools())}</b></div></div>')
        if waiting:
            trs = []
            for src, q, v in waiting[:5]:
                to = next((val for k, val in v["rows"] if k in ("To", "Will be delivered to", "Guests", "Conversation")), "")
                who = f" · by {q.requested_by}" if getattr(q, "requested_by", None) else ""
                sub = (f"To {to}" if to else "") + who + f" · {d.ago(q.created_at)}"
                key = f"{src.kind}:{q.id}"
                # The full request opens in place, so it can be approved here; one with a warning needs the Approvals page
                preview = (f'<dl>{"".join(f"<dt>{_esc(k)}</dt><dd>{_esc(val)}</dd>" for k, val in v["rows"])}</dl>'
                           + (f'<div class="message">{_esc(v["text"])}</div>' if v["text"] else "") + "".join(hint(x) for x in v["notes"]))
                if v["needs_override"] or v["warnings"]:
                    act = link_button("Review", f"/dashboard/approvals?item={key}")
                else:
                    act = (f'<form class="acts" method="post" action="/dashboard/approvals/act" data-title="{_esc(v["title"])}">{csrf}'
                           f'<input type="hidden" name="kind" value="{_esc(src.kind)}"><input type="hidden" name="id" value="{_esc(q.id)}">'
                           f'<input type="hidden" name="sha" value="{_esc(q.sha256)}"><input type="hidden" name="back" value="/dashboard">'
                           + button("Discard", name="action", value="discard") + button(_esc(src.approve_label_for(q) if hasattr(src, "approve_label_for") else src.approve_label), "primary", name="action", value="approve")
                           + "</form>")
                trs.append(f'<tr data-key="{_esc(key)}"><td><details><summary><b>{_esc(v["title"])}</b>'
                           f'<span class="row-sub">{_esc(sub.lstrip(" ·"))}</span></summary><div class="preview">{preview}</div></details></td>'
                           f'<td>{chip(v["badge"], "kind")}</td><td>{d.ttl(q)}</td><td>{act}</td></tr>')
            more = hint(f"{n - 5} more on Approvals.") if n > 5 else ""
            wait_body = ('<table class="waits"><thead><tr><th>Request</th><th>Kind</th><th>Time left</th><th></th></tr></thead>'
                         f'<tbody>{"".join(trs)}</tbody></table>{more}')
        else:
            wait_body = hint("When an agent sends mail, invites someone or writes an iMessage, it waits here until you decide.")
        left = [card("Waiting for your approval", wait_body, link=("Open Approvals", "/dashboard/approvals") if n else None)]
        entries = audit.entries(limit=5)
        acts = "".join(f'<li><time>{d.fmt_time(e.get("t"))}</time><span>{_entry_text(e)}</span></li>' for e in entries)
        left.append(card("Recent activity", f'<ol class="log">{acts}</ol>' if acts else hint("Nothing recorded yet."),
                         link=("All activity", "/dashboard/activity")))
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
        right = [card("Protection", prot, link=("All settings", "/dashboard/settings") if has_settings else None)]
        health = d.last_health
        if health is None:
            services = row("Services", value("Not checked yet"))
            state = ""
        else:
            services = "".join(
                row(area.replace("_", " ").capitalize(),
                    chip(f'OK · {r.get("ms", 0) / 1000:.1f} s', "ok") if r.get("ok") else chip("Failed", "err"),
                    "" if r.get("ok") else (r.get("error") or "")[:120])
                for area, r in (health.get("areas") or {}).items())
            state = chip("All working", "ok") if health.get("ok") else chip("Problem", "err")
        checked_at = hint(f'Checked {d.ago(health.get("_at"))}.') if health else ""
        server_rows = (f'<div class="list">{services}' + row("Version", value(version)) + "</div>" + checked_at
                       + hint("Pausing makes every tool answer that the server is paused. Nobody is signed out."))
        foot = (f'<form method="post" action="/dashboard/check">{csrf}{button("Check now")}</form>'
                f'<form method="post" action="/dashboard/pause">{csrf}<input type="hidden" name="paused" value="{"0" if paused else "1"}">'
                f'{button("Resume tools" if paused else "Pause tools", "primary" if paused else "")}</form>')
        right.append(card("Server", server_rows, foot, state or d.state_chip()))
        grid = (f'<div data-install-hint data-app="iCloud connector" data-lang="en" hidden></div>{kpis}'
                f'<div class="overview-grid"><div class="overview-col">{"".join(left)}</div>'
                f'<div class="overview-col">{"".join(right)}</div></div>')
        note = "" if request.headers.get(_LIVE) == "1" else flashes.pop(rec["csrf"], "")     # a background refresh takes no result
        return d.page("Overview", "overview", grid, rec, flash=note or _flash(request), wide=True, lede_html=lede, live="/dashboard")

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
    def approvals_page(rec: dict[str, Any], flash: str = "", bad: bool = False, status: int = 200, selected: str = "") -> Response:
        """The waiting requests as a list, and the chosen one (or the one dropped soonest) exactly as it will be sent."""
        waiting = []
        for src in d.sources.values():
            items = src.pending()
            ctx = src.contexts(items)
            waiting += [(src, q, src.describe(q, ctx.get(q.id))) for q in items]
        waiting.sort(key=lambda w: w[1].expires_at)
        total = len(waiting)
        recent_n = sum(time.time() - q.created_at < 3600 for _, q, _ in waiting)
        cards = []
        if recent_n > 2:
            cards.append(f'<p class="alert">Agents asked for {recent_n} of these in the last hour. Approve only what you asked for.</p>')
        if not total:
            cards.append(card("Nothing is waiting", hint("When an agent sends mail or invites someone, it waits here until you decide.")))
        else:
            chosen = next((w for w in waiting if f"{w[0].kind}:{w[1].id}" == selected), None)
            # A request picked from the list that has gone is not silently replaced by another one with a Send button in
            # the same place (dashboard.js reloads this page while it is open)
            src, q, v = (chosen or (None, None, None)) if selected else waiting[0]
            links = []
            for s2, q2, v2 in waiting:
                to = next((val for k, val in v2["rows"] if k in ("To", "Will be delivered to", "Guests", "Conversation")), "")
                cur = ' aria-current="true"' if q2 is q else ""
                links.append(f'<a href="/dashboard/approvals?item={_esc(s2.kind)}:{_esc(q2.id)}" data-key="{_esc(s2.kind)}:{_esc(q2.id)}"{cur}><span class="q1">{chip(v2["badge"], "kind")}'
                             f'<b>{_esc(v2["title"])}</b><span class="left">expires in {d.time_left(q2)}</span></span>'
                             + (f'<span class="q2">To {_esc(to)}</span>' if to else "") + "</a>")
            queue = f'<nav class="card queue" aria-label="Waiting requests">{"".join(links)}</nav>'
        if total and q is None:
            cards.append(f'<div class="split">{queue}' + card("No longer waiting", hint(
                "This request was sent, discarded or dropped, maybe in another window. Choose one from the list.")) + "</div>")
        elif total:
            body = [f'<dl>{"".join(f"<dt>{_esc(k)}</dt><dd>{_esc(val)}</dd>" for k, val in v["rows"])}</dl>']
            if v["warnings"]:
                body.append('<div class="alert" role="alert"><b>Check this first</b><ul>'
                            + "".join(f"<li>{_esc(w)}</li>" for w in v["warnings"]) + "</ul></div>")
            if v["text"]:
                body.append(f'<div class="message">{_esc(v["text"])}</div>')
            body += [hint(x) for x in v["notes"]]
            body.append(d.ttl(q, detail=True))
            who = f" by {q.requested_by}" if getattr(q, "requested_by", None) else ""
            body.append(f'<p class="meta">Requested {d.ago(q.created_at)}{_esc(who)}. If you do nothing, it is dropped and never sent.</p>')
            if v["needs_override"]:
                body.append('<label class="check"><input type="checkbox" name="override" value="1">'
                            '<span>I read the warning and still want this sent</span></label>')
            hidden = (d.csrf_input(rec) + f'<input type="hidden" name="kind" value="{_esc(src.kind)}">'
                      f'<input type="hidden" name="id" value="{_esc(q.id)}"><input type="hidden" name="sha" value="{_esc(q.sha256)}">')
            label = src.approve_label_for(q) if hasattr(src, "approve_label_for") else src.approve_label
            foot = button("Discard", name="action", value="discard") + button(_esc(label), "primary", name="action", value="approve")
            detail = card(v["title"], "".join(body), foot, chip(v["badge"], "kind"), form="/dashboard/approvals/act", hidden_html=hidden,
                          attrs={"data-key": f"{src.kind}:{q.id}", "data-title": v["title"]})
            cards.append(f'<div class="split">{queue}{detail}</div>')
        lede = f"{total} waiting. Nothing has left the server yet; what you see is exactly what will be sent." if total else ""
        # the request shown, also when none was picked: a refresh never puts another one in its place unasked
        shown = selected or (f"{src.kind}:{q.id}" if total and q is not None else "")
        live = "/dashboard/approvals" + (f"?item={quote(shown)}" if shown else "")
        return d.page("Approvals", "approvals", "".join(cards), rec, status, flash, bad, lede, wide=True, live=live)

    def after_action(rec: dict[str, Any], note: str, back: str) -> Response:
        while len(flashes) > 200:
            flashes.pop(next(iter(flashes)))
        flashes[rec["csrf"]] = note
        return RedirectResponse(back, status_code=303, headers=_HEADERS)

    @mcp.custom_route("/dashboard/approvals", methods=["GET"])
    async def approvals_get(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        note = "" if request.headers.get(_LIVE) == "1" else flashes.pop(got[1]["csrf"], "")   # a background refresh takes no result
        return await asyncio.to_thread(approvals_page, got[1], note, selected=request.query_params.get("item", ""))

    @mcp.custom_route("/dashboard/approvals/act", methods=["POST"])
    async def approvals_act(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        # dashboard.js asks for JSON and updates the page itself; a plain form post gets a page, as before
        as_json = "application/json" in request.headers.get("accept", "")
        back = "/dashboard" if str(form.get("back", "")) == "/dashboard" else "/dashboard/approvals"

        async def failed(note: str, json_status: int, page_status: int = 200) -> Response:
            if as_json:
                return JSONResponse({"ok": False, "error": note}, status_code=json_status, headers=_HEADERS)
            return await asyncio.to_thread(approvals_page, rec, note, True, page_status)

        def done(note: str) -> Response:
            if as_json:
                return JSONResponse({"ok": True, "note": note}, headers=_HEADERS)
            return after_action(rec, note, back)

        src = d.sources.get(str(form.get("kind", "")))
        action = str(form.get("action", ""))
        q = next((x for x in src.pending() if x.id == str(form.get("id", ""))), None) if src else None
        if q is None or action not in ("approve", "discard") or not hmac.compare_digest(str(form.get("sha", "")).encode(), q.sha256.encode()):
            return await failed("That item is no longer waiting or has changed. Nothing was done.", 409, 409)
        ctx = await asyncio.to_thread(src.contexts, [q], False)
        v = await asyncio.to_thread(src.describe, q, ctx.get(q.id))
        who = {"actor": "owner", "via": rec.get("via", "")}
        if action == "discard":
            await asyncio.to_thread(src.discard, q)
            audit.record("approval.discarded", **who, summary=f"Discarded {v['badge'].lower()} “{v['title']}”", kind=src.kind)
            return done("Discarded. Nothing was sent.")
        if v["needs_override"] and str(form.get("override", "")) != "1":
            return await failed("Not sent: tick the box under the warning if you still want it sent.", 422)
        try:
            note = await asyncio.to_thread(src.release, q)
        except Exception as e:  # noqa: BLE001  -- release() puts the item back; the owner can retry or discard
            log.warning("Release of %s %s failed: %s", src.kind, q.id, e)
            audit.record("approval.failed", **who, ok=False, summary=f"Could not send {v['badge'].lower()} “{v['title']}”", kind=src.kind)
            return await failed(f"Not sent (still waiting): {for_owner(str(e))}", 502)
        audit.record("approval.sent", **who, summary=f"Approved {v['badge'].lower()} “{v['title']}”", kind=src.kind,
                     recipients=[val for k, val in v["rows"] if k in ("To", "Guests", "Will be delivered to")])
        return done(note)

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
        cards.append(card("About this log", f"<p>{_esc(text)}</p>" + hint(f"Kept for {s.audit_keep_days} days. Names, addresses and subjects; "
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
        cards.append('<div class="phone-only">' + card("This browser", '<div class="list">' + row("Signed in", value(d.ago(rec.get("created"))), rec.get("via", "")) + "</div>"
                          + hint("Removing a passkey, changing the password and signing out apps ask you to confirm when your sign-in is older than 5 minutes."),
                          button("Sign out", "danger"), form="/dashboard/logout", hidden_html=d.csrf_input(rec)) + "</div>")
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

    # other parts (settings.py) build on the same guards
    d.signed_in, d.to_signin, d.checked_form, d.recent, d.confirm = signed_in, to_signin, checked_form, recent, confirm_page
    return d


_MESSAGES = {"checked": "Checked.", "paused": "Paused. Tools now answer that the server is paused.", "resumed": "Resumed.",
             "passkey_added": "Passkey added. You can now sign in with it.",
             "settings_saved": "Saved. The change applies when the server restarts."}


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
