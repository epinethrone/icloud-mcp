"""Settings on the dashboard: every setting of this server in one place, with where its value comes from.

The owner can change what the server does (areas, mail, calendar, contacts, the Mac helper, safety, the dashboard itself).
Where and how it listens, signs apps in and reaches its services stays in the environment and is shown read-only: a browser
session must never open a new OAuth redirect, a port or a bridge (config.DASHBOARD_EDITABLE is the list).

Changed values go to DATA_DIR/settings.json (mode 600) and win over the environment from the next start. Before anything is
saved the server builds its configuration with the new values and refuses a change it could not start with. A change that
weakens protection (sending directly, allowing invitations, emptying Trash, a wider allowlist, turning the log off...) asks
for the owner's passkey or password again. DASHBOARD_SETTINGS=view shows everything without changing it; =off ignores
settings.json altogether.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field as dfield
from typing import Any, Callable
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .config import DASHBOARD_EDITABLE, SETTINGS_FILE, Settings

log = logging.getLogger(__name__)
_TRUE = ("1", "true", "yes", "on")


@dataclass
class Spec:
    key: str
    label: str
    kind: str = "bool"            # bool | int | text | list | choice | textarea
    help: str = ""
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()   # (value, label)
    unit: int = 1                 # shown value = stored / unit (bytes -> MiB, seconds -> hours)
    unit_label: str = ""
    loosens: Callable[[str, str], bool] | None = None   # (old, new) -> True when the change weakens protection
    min: int | None = None
    max: int | None = None
    section: str = ""             # groups fields into cards on a settings page
    hidden: bool = False          # set through another control (the sending choice)


def _on(v: str) -> bool:
    return str(v).strip().lower() in _TRUE


def _to_true(old: str, new: str) -> bool:
    return not _on(old) and _on(new)


def _to_false(old: str, new: str) -> bool:
    return _on(old) and not _on(new)


def _higher(old: str, new: str) -> bool:
    try:
        return int(new or 0) > int(old or 0)
    except ValueError:
        return True


def _lower(old: str, new: str) -> bool:
    try:
        return int(new or 0) < int(old or 0)
    except ValueError:
        return True


def _wider(old: str, new: str) -> bool:
    """An allowlist is wider when it becomes empty (= anyone) or gains an entry."""
    o = {x.strip().lower() for x in old.split(",") if x.strip()}
    n = {x.strip().lower() for x in new.split(",") if x.strip()}
    return bool(o) and (not n or not n <= o)


def _changed(old: str, new: str) -> bool:
    return old.strip() != new.strip()


def _entries(v: str) -> set[str]:
    return {x.strip().lower() for x in v.split(",") if x.strip()}


def _removes_entry(old: str, new: str) -> bool:
    """A blocklist (chats never shown, people never messaged) is weaker as soon as an entry leaves it."""
    return not _entries(old) <= _entries(new)


def _adds_entry(old: str, new: str) -> bool:
    """A list that grants something (addresses that count as the owner's) is wider as soon as it gains an entry."""
    return not _entries(new) <= _entries(old)


def _more_history(old: str, new: str) -> bool:
    """An age limit where 0 means everything: it widens when it goes to 0 or above a limit that was set."""
    try:
        o, n = int(old or 0), int(new or 0)
    except ValueError:
        return True
    return o != 0 and (n == 0 or n > o)


@dataclass
class Group:
    slug: str
    title: str
    lede: str
    specs: list[Spec] = dfield(default_factory=list)
    needs_bridge: bool = False
    summary: Callable[[Callable[[str], str]], str] | None = None   # one line for the Settings list, from (key -> value)


GROUPS: list[Group] = [
    Group("accounts", "Account", "The Apple Account this server signs in to, and how your mail is signed.", [
        Spec("ICLOUD_EMAIL_ADDRESS", "From address", "text", "Your iCloud address or an alias; mail you send comes from it.",
             loosens=_changed),
        Spec("ICLOUD_DISPLAY_NAME", "Sender name", "text", "The name recipients see."),
        Spec("EMAIL_SIGNATURE", "Signature", "textarea", "Plain text added to the end of mail your agents send."),
    ], summary=lambda v: (f"Sends as {v('ICLOUD_DISPLAY_NAME')} " if v("ICLOUD_DISPLAY_NAME") else "Sends ")
                         + (f"from {v('ICLOUD_EMAIL_ADDRESS')}" if v("ICLOUD_EMAIL_ADDRESS") else "from the sign-in address")),
    Group("areas", "Areas and tools", "What your agents can reach at all. A switched-off area has no tools.", [
        Spec("ENABLE_MAIL", "Mail", default="true", loosens=_to_true),
        Spec("ENABLE_CALENDAR", "Calendar", default="true", loosens=_to_true),
        Spec("ENABLE_CONTACTS", "Contacts", default="true", loosens=_to_true),
        Spec("TOOLS", "Only these tools", "list", "Empty means every tool of the areas above. 'essential' is a small set; you can also "
             "name areas (mail, calendar) or tool names.", loosens=_changed),
    ], summary=lambda v: (", ".join(n for n, k in (("Mail", "ENABLE_MAIL"), ("Calendar", "ENABLE_CALENDAR"), ("Contacts", "ENABLE_CONTACTS"))
                                    if _on(v(k))) or "Nothing") + " on" + (f" · only {v('TOOLS')}" if v("TOOLS") else "")),
    Group("mail", "Mail", "How agents send and clean up mail.", [
        Spec("ALLOW_SEND", "Agents may send mail", default="true", loosens=_to_true, section="Sending", hidden=True),
        Spec("SEND_REQUIRES_APPROVAL", "Send only after my approval", default="true", loosens=_to_false, section="Sending", hidden=True),
        Spec("SEND_ALLOWLIST", "Only send to", "list", "Addresses or domains such as @example.org. Empty means anyone.", loosens=_wider,
             section="Sending"),
        Spec("MAX_RECIPIENTS", "Recipients per message", "int", default="25", min=1, max=500, loosens=_higher, section="Sending"),
        Spec("SAVE_SENT_COPY", "Keep a copy in Sent", default="true", section="Sending"),
        Spec("OUTBOX_TTL_SECONDS", "Waits for approval", "int", "After this long an unapproved message is dropped, never sent.",
             default="86400", unit=3600, unit_label="hours", min=1, max=720, section="Approval queue"),
        Spec("OUTBOX_MAX", "Messages waiting at most", "int", default="20", min=1, max=500, loosens=_higher, section="Approval queue"),
        Spec("MAX_ATTACHMENT_BYTES", "Largest attachment", "int", default=str(5 * 1024 * 1024), unit=1024 * 1024, unit_label="MiB",
             min=1, max=100, loosens=_higher, section="Reading and cleanup"),
        Spec("MAIL_MAX_AGE_DAYS", "Only mail from the last", "int", "0 means the whole mailbox.", default="0", min=0, max=36500,
             unit_label="days", loosens=_more_history, section="Reading and cleanup"),
        Spec("MAX_BODY_CHARS", "Longest text shown to agents", "int", default="30000", min=1000, max=500000, unit_label="characters",
             loosens=_higher, section="Reading and cleanup"),
        Spec("ALLOW_PERMANENT_DELETE", "Allow emptying Trash", default="false",
             help="Mail deleted from Trash cannot be recovered.", loosens=_to_true, section="Reading and cleanup"),
    ], summary=lambda v: ("Off (read-only)" if _on(v("READ_ONLY")) else "Drafts only" if not _on(v("ALLOW_SEND"))
                          else "Sends after your approval" if _on(v("SEND_REQUIRES_APPROVAL")) else "Sends directly")
                         + f" · attachments up to {int(v('MAX_ATTACHMENT_BYTES') or 0) // 1048576} MiB"),
    Group("calendar", "Calendar", "Time zone, where new events go, and whether agents may invite people.", [
        Spec("DEFAULT_TIMEZONE", "Time zone", "text", "An IANA name such as Europe/Amsterdam, for times without an offset.", default="UTC",
             section="General"),
        Spec("DEFAULT_CALENDAR", "Calendar for new events", "text", "Empty: “Calendar” or “Home”, else the first.", section="General"),
        Spec("OWNER_ADDRESSES", "My other addresses", "list", "Aliases, so invitations to them count as yours.", loosens=_adds_entry,
             section="General"),
        Spec("ALLOW_CALENDAR_INVITES", "Invitations to other people", "choice",
             "Events with guests make iCloud email them: invitations, updates and cancellations.", default="false",
             choices=(("false", "Off"), ("true", "Allowed")), loosens=_to_true, section="Invitations"),
        Spec("INVITE_ALLOWLIST", "Only invite", "list", "Addresses or domains. Empty means anyone.", loosens=_wider, section="Invitations"),
        Spec("MAX_ATTENDEES", "Guests per event", "int", default="10", min=1, max=200, loosens=_higher, section="Invitations"),
    ], summary=lambda v: f"{v('DEFAULT_TIMEZONE') or 'UTC'} · invitations "
                         + ("allowed" if _on(v("ALLOW_CALENDAR_INVITES")) else "off")),
    Group("contacts", "Contacts", "What agents may change on contact cards.", [
        Spec("CONTACTS_ALLOW_EMAIL_CHANGES", "Agents may add or change addresses and numbers", default="true",
             help="Off stops a planted address on a real contact.", loosens=_to_true),
    ], summary=lambda v: "Agents may change addresses and numbers" if _on(v("CONTACTS_ALLOW_EMAIL_CHANGES")) else "Addresses and numbers are protected"),
    Group("helper", "Mac helper", "Reminders, Notes, iCloud Drive, Maps, Messages and Health through your Mac.", [
        Spec("ENABLE_REMINDERS", "Reminders", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_NOTES", "Notes", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_DRIVE", "iCloud Drive", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_MAPS", "Maps", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_IMESSAGE", "Read Messages", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_HEALTH", "Health", default="false", loosens=_to_true, section="Areas"),
        Spec("IMESSAGE_ALLOW_SEND", "Agents may send iMessages", default="false", loosens=_to_true, section="Messages"),
        Spec("IMESSAGE_SEND_REQUIRES_APPROVAL", "Send iMessages only after my approval", default="true", loosens=_to_false, section="Messages"),
        Spec("IMESSAGE_SEND_ALLOWLIST", "Who may receive an iMessage", "list", "Empty means nobody, * anyone.", loosens=_changed, section="Messages"),
        Spec("IMESSAGE_NEVER_SEND", "Never send to", "list", loosens=_removes_entry, section="Messages"),
        Spec("IMESSAGE_HIDDEN_CHATS", "Chats agents never see", "list", loosens=_removes_entry, section="Messages"),
        Spec("IMESSAGE_VISIBLE_CHATS", "Only these chats", "list", "Empty means every chat except the hidden ones.", loosens=_changed, section="Messages"),
        Spec("IMESSAGE_MAX_AGE_DAYS", "Only messages from the last", "int", "0 means the whole history.", default="0", min=0, max=36500, unit_label="days",
             loosens=_more_history, section="Messages"),
        Spec("IMESSAGE_HIDE_SHORT_CODES", "Hide service senders", default="true", help="Banks, deliveries, one-time codes.", loosens=_to_false, section="Messages"),
        Spec("SHORTCUTS_ALLOW", "Shortcuts agents may run", "list", "Exact names; the Mac keeps its own list too.", loosens=_changed, section="Shortcuts and timing"),
        Spec("BRIDGE_JOB_TIMEOUT_SECONDS", "Wait for the Mac", "int", default="60", min=5, max=600, unit_label="seconds", section="Shortcuts and timing"),
    ], needs_bridge=True, summary=lambda v: (", ".join(n for n, k in (("Reminders", "ENABLE_REMINDERS"), ("Notes", "ENABLE_NOTES"),
                                                                       ("Drive", "ENABLE_DRIVE"), ("Maps", "ENABLE_MAPS"),
                                                                       ("Messages", "ENABLE_IMESSAGE"), ("Health", "ENABLE_HEALTH"))
                                                    if _on(v(k))) or "Nothing") + " on"),
    Group("safety", "Safety", "Switches that limit everything at once.", [
        Spec("READ_ONLY", "Read-only server", default="false", help="Nothing can be sent, moved, changed or deleted.", loosens=_to_false),
    ], summary=lambda v: "Read-only: nothing can change" if _on(v("READ_ONLY")) else "Agents may make changes"),
    Group("dashboard", "Dashboard and log", "How this dashboard signs you in and how long activity is kept.", [
        Spec("OWNER_PASSWORD_LOGIN", "Allow sign-in with the owner password", default="true",
             help="Turn it off once you have a passkey.", loosens=_to_true, section="Sign-in"),
        Spec("DASHBOARD_SESSION_MINUTES", "Sign out after", "int", "Minutes without use.", default="30", min=5, max=720, unit_label="minutes", loosens=_higher, section="Sign-in"),
        Spec("AUDIT_LOG", "Keep an activity log", default="true", loosens=_to_false, section="Activity log"),
        Spec("AUDIT_KEEP_DAYS", "Keep activity for", "int", default="90", min=1, max=3650, unit_label="days", loosens=_lower, section="Activity log"),
    ], summary=lambda v: ("Password sign-in on" if _on(v("OWNER_PASSWORD_LOGIN")) else "Passkey only")
                         + (f" · activity kept {v('AUDIT_KEEP_DAYS')} days" if _on(v("AUDIT_LOG")) else " · no activity log")),
]
SPECS = {sp.key: sp for g in GROUPS for sp in g.specs}
assert set(SPECS) <= DASHBOARD_EDITABLE, set(SPECS) - DASHBOARD_EDITABLE

# Shown read-only under "Server": (label, environment variable, how to show it)
SERVER_VIEW = [
    ("Address", [("Public address", "MCP_PUBLIC_URL", "text"), ("Listens on", ("MCP_HOST", "MCP_PORT"), "hostport"),
                 ("Extra allowed hosts", "MCP_EXTRA_ALLOWED_HOSTS", "text")]),
    ("App sign-in", [("Apps may return to", "OAUTH_ALLOWED_REDIRECT_HOSTS", "text"), ("Access token lifetime (s)", "ACCESS_TOKEN_TTL", "text"),
                     ("Refresh token lifetime (s)", "REFRESH_TOKEN_TTL", "text")]),
    ("Connections", [("IMAP", ("IMAP_HOST", "IMAP_PORT"), "hostport"), ("SMTP", ("SMTP_HOST", "SMTP_PORT"), "hostport"),
                     ("CalDAV", "CALDAV_URL", "text"), ("CardDAV", "CARDDAV_URL", "text"), ("Sign-in name", "ICLOUD_USERNAME", "text")]),
    ("Performance", [("Tool time limit (s)", "TOOL_TIMEOUT_SECONDS", "text"), ("Parallel tool calls", "TOOL_WORKERS", "text"),
                     ("Mail connections kept", "IMAP_POOL_SIZE", "text"), ("Calendar connections kept", "CALDAV_POOL_SIZE", "text")]),
    ("Other", [("Mac helper bridge", "BRIDGE_TOKEN", "set"), ("Menu bar admin API port", "ADMIN_PORT", "text"),
               ("Own safety screen", "SAFETY_SCREEN", "set"), ("Owner's rules for agents", "AGENT_NOTES_FILE", "set"),
               ("Data folder", "DATA_DIR", "text"), ("Log level", "LOG_LEVEL", "text")]),
]


class SettingsStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, SETTINGS_FILE)
        self._lock = threading.Lock()

    def read(self) -> dict[str, str]:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if k in DASHBOARD_EDITABLE and isinstance(v, str)} if isinstance(data, dict) else {}

    def write(self, values: dict[str, str]) -> None:
        from .admin import _write_private
        with self._lock:
            _write_private(self.path, json.dumps(dict(sorted(values.items())), indent=1) + "\n")

    def update(self, set_: dict[str, str] | None = None, remove: tuple[str, ...] = ()) -> dict[str, str]:
        """Apply only these keys to what is on disk now, read under the lock, so two saves at once cannot lose a change."""
        from .admin import _write_private
        with self._lock:
            values = {k: v for k, v in {**self.read(), **(set_ or {})}.items() if k not in remove}
            _write_private(self.path, json.dumps(dict(sorted(values.items())), indent=1) + "\n")
            return values


def current(key: str, stored: dict[str, str]) -> tuple[str, str]:
    """(value, source) where source is 'dashboard', 'environment' or 'default'."""
    if key in stored:
        return stored[key], "dashboard"
    env = os.environ.get(key)
    if env is not None and env.strip() != "":
        return env.strip(), "environment"
    return SPECS[key].default if key in SPECS else "", "default"


def parse(sp: Spec, form: Any) -> str:
    """The submitted value as the environment would hold it. Raises ValueError with a message for the owner."""
    raw = form.get(sp.key)
    if sp.kind == "bool":
        return "true" if raw is not None and str(raw) in ("1", "true", "on") else "false"
    v = str(raw or "").strip()
    if sp.kind == "int":
        try:
            n = float(v.replace(",", ".")) if v else 0
        except ValueError:
            raise ValueError(f"{sp.label}: enter a number.") from None
        if (sp.min is not None and n < sp.min) or (sp.max is not None and n > sp.max):
            raise ValueError(f"{sp.label}: between {sp.min} and {sp.max}{' ' + sp.unit_label if sp.unit_label else ''}.")
        return str(int(round(n * sp.unit)))
    if sp.kind == "choice":
        if v not in {c for c, _ in sp.choices}:
            raise ValueError(f"{sp.label}: pick one of the options.")
        return v
    if sp.kind == "list":
        return ",".join(x.strip() for x in v.replace("\n", ",").split(",") if x.strip())
    if sp.kind == "textarea":
        return v.replace("\r\n", "\n").replace("\n", "\\n")[:2000]
    return v[:300]


def validate(new_values: dict[str, str], known_tools: set[str]) -> tuple[str, str] | None:
    """Would the server start with these values? Returns (setting, reason) when it would not, the reason in the owner's words;
    the setting is "" when the server cannot tell which one is wrong."""
    if tz := new_values.get("DEFAULT_TIMEZONE"):
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            return "DEFAULT_TIMEZONE", f"“{tz}” is not a time zone name. Use one such as Europe/Amsterdam or America/New_York."
    if tools := new_values.get("TOOLS"):
        from .server import AREA_PRESETS, RENAMED
        unknown = [t for t in tools.split(",") if t and t != "essential" and t not in AREA_PRESETS and t not in RENAMED and t not in known_tools]
        if unknown:
            return "TOOLS", "Unknown tool names: " + ", ".join(unknown) + ". The server would not start with them."
    try:
        s = Settings.from_env(dashboard_values=new_values)
        s.validate_for_server()
    except SystemExit as e:
        return "", str(e)
    except ValueError as e:
        return "", f"A value does not parse: {e}"
    return None


def register_settings(d: Any, mcp: Any, s: Settings) -> None:
    """Adds Settings to the dashboard `d` (dashboard.Dashboard)."""
    from . import dashboard as D

    store = SettingsStore(s.data_dir)
    editable = s.dashboard_settings == "edit"
    d.extra_nav.append(("settings", "Settings", "/dashboard/settings"))
    started_with = dict(s.settings_active and store.read() or {})
    state = {"restart_needed": False}

    def known_tools() -> set[str]:
        return {t.name for t in mcp._tool_manager.list_tools()}

    def group_summary(g: Group, stored: dict[str, str]) -> str:
        parts = []
        for sp in g.specs[:3]:
            v, _ = current(sp.key, stored)
            parts.append(f"{sp.label}: {display(sp, v)}")
        return " · ".join(parts)

    def display(sp: Spec, v: str) -> str:
        if sp.kind == "bool":
            return "on" if _on(v) else "off"
        if sp.kind == "choice":
            return dict(sp.choices).get(v.lower(), v)
        if sp.kind == "int" and sp.unit != 1:
            try:
                return f"{int(v) / sp.unit:g} {sp.unit_label}"
            except ValueError:
                return v
        if sp.kind == "int" and sp.unit_label:
            return f"{v} {sp.unit_label}"
        return v or "(empty)"

    def control(sp: Spec, v: str, src: str, typed: str | None = None, error: str = "") -> str:
        dis = "" if editable else " disabled"
        sub = D._esc(sp.help)
        tag = "Set in the environment" if src == "environment" else ""   # values set here are listed in the card below
        meta = f'<p class="hint">{sub}{" · " if sub and tag else ""}{tag}</p>' if sub or tag else ""
        fid = f"f-{sp.key.lower()}"
        if error:
            meta = f'<p class="field-error" id="{fid}-err">{D._esc(error)}</p>' + meta
            dis += f' aria-invalid="true" aria-describedby="{fid}-err"'
        if typed is not None:
            v = typed
        if sp.kind == "bool":
            return (f'<div class="switch-row"><label for="{fid}"><span>{D._esc(sp.label)}</span>{meta}</label>'
                    f'<input id="{fid}" class="switch" type="checkbox" role="switch" name="{sp.key}" value="true"{" checked" if _on(v) else ""}{dis}></div>')
        if sp.kind == "choice" and len(sp.choices) <= 4:
            # Up to four short, equal options are shown side by side (a segmented control), so every choice is visible at once
            opts = "".join(f'<label><input type="radio" name="{sp.key}" value="{D._esc(c)}"{" checked" if c == v.lower() else ""}{dis}>'
                           f'<span>{D._esc(lbl)}</span></label>' for c, lbl in sp.choices)
            return (f'<div class="field"><span class="field-label" id="{fid}">{D._esc(sp.label)}</span>'
                    f'<div class="radio-seg" role="radiogroup" aria-labelledby="{fid}">{opts}</div>{meta}</div>')
        if sp.kind == "choice":
            opts = "".join(f'<option value="{D._esc(c)}"{" selected" if c == v.lower() else ""}>{D._esc(lbl)}</option>' for c, lbl in sp.choices)
            inp = f'<select id="{fid}" name="{sp.key}"{dis}>{opts}</select>'
        elif sp.kind == "textarea":
            inp = f'<textarea id="{fid}" name="{sp.key}" rows="3"{dis}>{D._esc(v.replace(chr(92) + "n", chr(10)))}</textarea>'
        elif sp.kind == "int":
            shown = v
            if sp.unit != 1 and typed is None:
                try:
                    shown = f"{int(v) / sp.unit:g}"
                except ValueError:
                    pass
            unit = f'<span class="hint">{D._esc(sp.unit_label)}</span>' if sp.unit_label else ""
            inp = (f'<span style="display:flex;align-items:center;gap:8px"><input id="{fid}" name="{sp.key}" type="number" inputmode="numeric" '
                   f'value="{D._esc(shown)}" min="{sp.min if sp.min is not None else ""}" max="{sp.max if sp.max is not None else ""}" '
                   f'style="max-width:9rem"{dis}>{unit}</span>')
        else:
            inp = f'<input id="{fid}" name="{sp.key}" type="text" value="{D._esc(v)}" autocomplete="off"{dis}>'
        return f'<div class="field"><label for="{fid}">{D._esc(sp.label)}</label>{inp}{meta}</div>'

    def send_mode(stored: dict[str, str], entered: dict[str, str]) -> str:
        allow = entered.get("ALLOW_SEND") or current("ALLOW_SEND", stored)[0]
        appr, src = current("SEND_REQUIRES_APPROVAL", stored)
        appr = entered.get("SEND_REQUIRES_APPROVAL") or appr
        mode = "drafts" if not _on(allow) else ("approval" if _on(appr) else "direct")
        dis = "" if editable else " disabled"
        opts = "".join(f'<label><input type="radio" name="SEND_MODE" value="{val}"{" checked" if val == mode else ""}{dis}><span>{label}</span></label>'
                       for val, label in (("drafts", "Drafts only"), ("approval", "After approval"), ("direct", "Directly")))
        explain = {"drafts": "Agents save drafts; you send them in Mail.", "approval": "Agents queue mail; you send it on Approvals.",
                   "direct": "Mail leaves at once. Your client's own prompt is the only check."}[mode]
        tag = "Set in the environment" if src == "environment" else ""
        return (f'<div class="field"><span class="field-label" id="sendmode">When agents send mail</span>'
                f'<div class="radio-seg" role="radiogroup" aria-labelledby="sendmode">{opts}</div><p class="hint">{explain}{" · " + tag if tag else ""}</p></div>')

    def area_tabs(current_slug: str) -> str:
        """The settings areas as underline tabs, so the owner moves between them without going back to the list."""
        areas = [(g.slug, g.title) for g in GROUPS] + [("server", "Server")]
        links = "".join(f'<a href="/dashboard/settings/{slug}"{" aria-current=page" if slug == current_slug else ""}>{D._esc(title)}</a>'
                        for slug, title in areas)
        return f'<nav class="subtabs" aria-label="Settings areas">{links}</nav>'

    def index_page(rec: dict[str, Any], flash: str = "", bad: bool = False) -> Response:
        stored = store.read()
        cards = []
        if state["restart_needed"] or stored != started_with:
            cards.append(D.card("Restart to apply", D.hint("Saved changes take effect when the server starts again. Agents are "
                                "disconnected for about half a minute."), D.button("Restart now", "primary"),
                                D.chip("Waiting", "warn"), form="/dashboard/settings/restart", hidden_html=d.csrf_input(rec)))
        if not editable:
            cards.append('<p class="alert">Settings can only be viewed here (DASHBOARD_SETTINGS=view). Change them in the server’s environment.</p>')
        rows = []
        for g in GROUPS:
            if g.needs_bridge and not s.bridge_token:
                sub = "Not set up. See the Mac helper guide."
            else:
                sub = g.summary(lambda k: current(k, stored)[0]) if g.summary else group_summary(g, stored)
            rows.append(f'<a class="row" href="/dashboard/settings/{g.slug}" style="color:inherit;text-decoration:none">'
                        f'<span class="row-main"><span>{D._esc(g.title)}</span><span class="row-sub">{D._esc(sub)}</span></span>'
                        f'<span aria-hidden="true" class="hint">›</span></a>')
        rows.append('<a class="row" href="/dashboard/settings/server" style="color:inherit;text-decoration:none"><span class="row-main">'
                    '<span>Server</span><span class="row-sub">Address, app sign-in, connections, performance · view only</span></span>'
                    '<span aria-hidden="true" class="hint">›</span></a>')
        cards.append(D.card("", f'<div class="list">{"".join(rows)}</div>'))
        cards.append(D.hint("Values changed here are stored in the server's data folder and win over its environment. "
                            "Every change is recorded in Activity."))
        return d.page("Settings", "settings", "".join(cards), rec, flash=flash, bad=bad, lede="Everything this server does, in one place.")

    def group_page(rec: dict[str, Any], g: Group, flash: str = "", bad: bool = False, status: int = 200,
                   entered: dict[str, str] | None = None, typed: dict[str, str] | None = None,
                   errors: dict[str, str] | None = None) -> Response:
        """After a refused save, entered holds the values that parsed, typed the raw text of those that did not, and errors
        the message per setting; the form shows them instead of the saved values, so nothing the owner typed is lost."""
        stored = store.read()
        entered, typed, errors = entered or {}, typed or {}, errors or {}
        if g.needs_bridge and not s.bridge_token:
            body = D.hint("These areas work through a small helper on your Mac. Set BRIDGE_TOKEN and follow the Mac helper guide first; "
                          "then they can be switched on here.")
            return d.page(g.title, "settings", D.card("Not set up", body, D.link_button("Back to Settings", "/dashboard/settings")),
                          rec, status, flash, bad, g.lede)
        sections: dict[str, list[str]] = {}
        for sp in g.specs:
            if sp.hidden:
                continue
            v, src = current(sp.key, stored)
            sections.setdefault(sp.section, []).append(control(sp, entered.get(sp.key, v), src, typed.get(sp.key), errors.get(sp.key, "")))
        if g.slug == "mail":
            sections.setdefault("Sending", []).insert(0, send_mode(stored, entered))
        foot = ('<p class="foot-hint">Saved changes apply after a restart.</p>' + D.button("Save", "primary")) if editable \
            else '<p class="foot-hint">These settings can only be viewed here.</p>' 
        names = list(sections)
        # each section is a row: its name on the left, its fields on the right (stacked on a phone); one Save at the end
        inner = "".join(f'<div class="srow"><div class="srow-head"><h2>{D._esc(name)}</h2></div><section class="card">'
                        + f'<div class="card-body">{"".join(items)}</div>'
                        + (f'<div class="card-foot">{foot}</div>' if i == len(names) - 1 else "") + "</section></div>"
                        for i, (name, items) in enumerate(sections.items()))
        cards = [area_tabs(g.slug)]
        cards.append(f'<form class="stack" method="post" action="/dashboard/settings/{g.slug}">{d.csrf_input(rec)}{inner}</form>' if editable
                     else f'<div class="stack">{inner}</div>')
        changed = [sp for sp in g.specs if sp.key in stored]
        if changed and editable:
            rows = "".join(D.row(sp.label, f'<form method="post" action="/dashboard/settings/{g.slug}/reset">{d.csrf_input(rec)}'
                                           f'<input type="hidden" name="key" value="{sp.key}">{D.button("Reset", "text")}</form>',
                                 f"Here: {display(sp, stored[sp.key])}") for sp in changed)
            cards.append(D.card("Set here", f'<div class="list">{rows}</div>'
                                + D.hint("Reset goes back to the server's environment or the default.")))
        if g.slug == "accounts" and editable:
            cards.append(D.card("App-specific password", D.field("apppw", "New app-specific password",
                                'type="password" name="password" autocomplete="off" placeholder="abcd-efgh-ijkl-mnop" required',
                                "Create it at account.apple.com. iCloud checks it before anything is saved."),
                                D.button("Replace password"), form="/dashboard/settings/accounts/app-password", hidden_html=d.csrf_input(rec)))
        return d.page(g.title, "settings", "".join(cards), rec, status, flash, bad, g.lede)

    def server_page(rec: dict[str, Any]) -> Response:
        cards = [area_tabs("server"), '<p class="banner">These are set in the server’s environment by whoever runs it. A browser cannot change them.</p>']
        for title, items in SERVER_VIEW:
            rows = []
            for label, key, how in items:
                if how == "hostport":
                    val = ":".join(os.environ.get(k, "") or "default" for k in key)
                elif how == "set":
                    val = "set" if os.environ.get(key, "").strip() else "not set"
                else:
                    val = os.environ.get(key, "").strip() or "default"
                rows.append(D.row(label, D.value(val)))
            cards.append(D.card(title, f'<div class="list">{"".join(rows)}</div>'))
        return d.page("Server", "settings", "".join(cards), rec, lede="Where this server listens, how apps sign in, and what it connects to.")

    def checked(fn):
        async def wrapper(request: Request) -> Response:
            got = d.signed_in(request)
            if got is None:
                return d.to_signin(request)
            return await fn(request, got[1])
        return wrapper

    @mcp.custom_route("/dashboard/settings", methods=["GET"])
    @checked
    async def settings_index(request: Request, rec) -> Response:
        return index_page(rec, D._flash(request))

    @mcp.custom_route("/dashboard/settings/server", methods=["GET"])
    @checked
    async def settings_server(request: Request, rec) -> Response:
        return server_page(rec)

    @mcp.custom_route("/dashboard/settings/{slug}", methods=["GET"])
    @checked
    async def settings_group(request: Request, rec) -> Response:
        g = next((x for x in GROUPS if x.slug == request.path_params["slug"]), None)
        if g is None:
            return d.message(rec, "Not found", "There is no such settings page.", 404)
        return group_page(rec, g)

    # A change that weakens protection waits here while the owner confirms it's them, so it need not be entered again.
    held: dict[str, tuple[float, str, str, dict[str, str]]] = {}     # session csrf -> (when, group slug, "save" | "reset", form)

    def hold(rec: dict[str, Any], g: Group, action: str, form: dict[str, str], lede: str) -> Response:
        while len(held) > 200:
            held.pop(next(iter(held)))
        held[rec["csrf"]] = (time.time(), g.slug, action, form)
        return d.confirm(rec, f"/dashboard/settings/{g.slug}/confirmed", lede)

    def read_form(g: Group, form: dict[str, str], stored: dict[str, str]):
        if g.slug == "mail" and form.get("SEND_MODE") in ("drafts", "approval", "direct"):
            mode = form.get("SEND_MODE")
            form = {**form, "ALLOW_SEND": "true" if mode != "drafts" else "", "SEND_REQUIRES_APPROVAL": "true" if mode != "direct" else ""}
            form = {k: v for k, v in form.items() if v != ""}
        new, changes, looser, entered, typed, errors = dict(stored), [], [], {}, {}, {}
        for sp in g.specs:
            old, _src = current(sp.key, stored)
            try:
                val = parse(sp, form)
            except ValueError as e:
                typed[sp.key], errors[sp.key] = str(form.get(sp.key) or ""), str(e)
                continue
            entered[sp.key] = val
            if val == old:
                continue
            new[sp.key] = val
            changes.append((sp, old, val))
            if sp.loosens and sp.loosens(old, val):
                looser.append(sp)
        return new, changes, looser, entered, typed, errors

    async def save(rec: dict[str, Any], g: Group, form: dict[str, str]) -> Response:
        new, changes, looser, entered, typed, errors = read_form(g, form, store.read())
        if errors:
            first = next(iter(errors.values()))
            more = f" ({len(errors) - 1} more marked below)" if len(errors) > 1 else ""
            return group_page(rec, g, f"Not saved: {first}{more}", True, 400, entered, typed, errors)
        if not changes:
            return group_page(rec, g, "Nothing changed.")
        if looser and not d.recent(rec):
            return hold(rec, g, "save", form, "This change weakens protection (" + ", ".join(sp.label for sp in looser)
                        + "), so it asks you to sign in again. It is saved as soon as you have.")
        if problem := await asyncio.to_thread(validate, new, known_tools()):
            key, why = problem
            return group_page(rec, g, f"Not saved: {why}", True, 400, entered, errors={key: why} if key else None)
        store.update({sp.key: v for sp, _, v in changes})
        state["restart_needed"] = True
        d.audit.record("settings.changed", actor="owner", via=rec.get("via", ""), ok=True,
                       summary=f"Changed {g.title.lower()} settings: " + ", ".join(f"{sp.label} {display(sp, o)} → {display(sp, v)}" for sp, o, v in changes),
                       keys=[sp.key for sp, _, _ in changes], weakened=[sp.key for sp in looser])
        log.warning("Owner changed %s from the dashboard; they apply after a restart", ", ".join(sp.key for sp, _, _ in changes))
        return RedirectResponse("/dashboard/settings?m=settings_saved", status_code=303, headers=D._HEADERS)

    async def reset(rec: dict[str, Any], g: Group, key: str) -> Response:
        stored = store.read()
        if key not in stored or key not in {sp.key for sp in g.specs}:
            return d.message(rec, "Not found", "That setting was not changed here.", 404)
        sp = SPECS[key]
        new = {k: v for k, v in stored.items() if k != key}
        back, _ = current(key, new)
        if sp.loosens and sp.loosens(stored[key], back) and not d.recent(rec):
            return hold(rec, g, "reset", {"key": key}, "Going back to the server's value weakens protection, so it asks you to sign in "
                        "again. It is reset as soon as you have.")
        if problem := await asyncio.to_thread(validate, new, known_tools()):
            return group_page(rec, g, f"Not reset: {problem[1]}", True, 400)
        store.update(remove=(key,))
        state["restart_needed"] = True
        d.audit.record("settings.changed", actor="owner", via=rec.get("via", ""),
                       summary=f"Reset {sp.label} to {display(sp, back)}", keys=[key])
        return group_page(rec, g, f"{sp.label} now follows the server again ({display(sp, back)}). Restart to apply.")

    def group_for(request: Request) -> Group | None:
        g = next((x for x in GROUPS if x.slug == request.path_params["slug"]), None)
        return g if g is not None and editable else None

    # Registered before /dashboard/settings/{slug}: routes match in order, and "restart" would be taken for a group name.
    @mcp.custom_route("/dashboard/settings/restart", methods=["POST"])
    async def restart(request: Request) -> Response:
        got = await d.checked_form(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        d.audit.record("server.restart", actor="owner", via=rec.get("via", ""), summary="Restarted the server to apply settings")
        log.warning("Owner asked for a restart from the dashboard")
        threading.Timer(0.7, d.exit_fn).start()           # after the page has gone out; Docker or launchd starts it again
        return d.page("Restarting", "settings", D.card("Restarting", D.hint("The server starts again in about half a minute. "
                                                                             "Reload this page then."), D.link_button("Reload", "/dashboard/settings")),
                      rec, lede="")

    @mcp.custom_route("/dashboard/settings/{slug}", methods=["POST"])
    async def settings_save(request: Request) -> Response:
        got = await d.checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        g = group_for(request)
        if g is None:
            return d.message(rec, "Not allowed", "These settings cannot be changed here.", 403)
        return await save(rec, g, {k: str(v) for k, v in form.items() if k != "csrf"})

    @mcp.custom_route("/dashboard/settings/{slug}/reset", methods=["POST"])
    async def settings_reset(request: Request) -> Response:
        got = await d.checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        g = group_for(request)
        if g is None:
            return d.message(rec, "Not found", "That setting was not changed here.", 404)
        return await reset(rec, g, str(form.get("key", "")))

    @mcp.custom_route("/dashboard/settings/{slug}/confirmed", methods=["GET"])
    @checked
    async def settings_confirmed(request: Request, rec) -> Response:
        """Where the confirm card returns to, after signing in again or after Cancel. Only a change this session submitted
        itself (with its CSRF token) in the last ten minutes is applied, once."""
        g = group_for(request)
        item = held.pop(rec["csrf"], None)
        if g is None or item is None or item[1] != g.slug or time.time() - item[0] > 600:
            return RedirectResponse(f"/dashboard/settings/{request.path_params['slug']}", status_code=303, headers=D._HEADERS)
        _, _, action, form = item
        if not d.recent(rec):                                    # Cancel: show the change again, unsaved
            if action == "reset":
                return group_page(rec, g, "Not reset: it was not confirmed.", True)
            _, _, _, entered, typed, _ = read_form(g, form, store.read())
            return group_page(rec, g, "Not saved: it was not confirmed. Save again to confirm.", True, 200, entered, typed)
        return await (save(rec, g, form) if action == "save" else reset(rec, g, form.get("key", "")))

    @mcp.custom_route("/dashboard/settings/accounts/app-password", methods=["POST"])
    async def app_password(request: Request) -> Response:
        got = await d.checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        g = next(x for x in GROUPS if x.slug == "accounts")
        if not editable:
            return d.message(rec, "Not allowed", "These settings cannot be changed here.", 403)
        if not d.recent(rec):
            return d.confirm(rec, "/dashboard/settings/accounts", "Replacing the app password asks you to sign in again.")
        value = str(form.get("password", "")).replace(" ", "").replace("-", "")
        if len(value) != 16:
            return group_page(rec, g, "An app-specific password has 16 letters, like abcd-efgh-ijkl-mnop.", True, 400)
        value = "-".join(value[i:i + 4] for i in range(0, 16, 4))
        from .mail import MailService
        try:
            c = await asyncio.to_thread(MailService(dataclasses.replace(s, app_password=value))._login)
            await asyncio.to_thread(c.logout)
        except Exception:  # noqa: BLE001 - the reason can quote the account; say only that it failed
            return group_page(rec, g, "iCloud did not accept this password. Nothing was changed.", True, 400)
        from .admin import save_override
        save_override(s.data_dir, "icloud_app_password", value)
        state["restart_needed"] = True
        d.audit.record("settings.changed", actor="owner", via=rec.get("via", ""), summary="Replaced the iCloud app-specific password")
        return RedirectResponse("/dashboard/settings?m=settings_saved", status_code=303, headers=D._HEADERS)
