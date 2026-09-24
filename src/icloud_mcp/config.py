"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else int(v)


def keychain_password(account: str, service: str = "icloud-mcp") -> str:
    """The app-specific password stored in the macOS login Keychain (see `icloud-mcp --store-password`), or ''.
    Only consulted when ICLOUD_APP_PASSWORD is not set, so the password never has to sit in a file or a client config."""
    import subprocess
    import sys

    if sys.platform != "darwin" or not account or os.environ.get("ICLOUD_KEYCHAIN", "true").lower() in ("0", "false", "no", "off"):
        return ""
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in _str(name, default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # --- iCloud credentials -------------------------------------------------
    username: str                 # Apple ID used to log in
    app_password: str             # app-specific password (xxxx-xxxx-xxxx-xxxx)
    email_address: str            # From address (defaults to username)
    display_name: str
    signature: str                # plain-text signature appended to outgoing mail
    # --- mail transport -------------------------------------------------------
    imap_host: str
    imap_port: int
    imap_security: str            # ssl | starttls | none
    imap_username: str
    smtp_host: str
    smtp_port: int
    smtp_security: str            # starttls | ssl | none
    smtp_username: str
    save_sent_copy: bool
    allow_permanent_delete: bool
    max_recipients: int
    send_allowlist: tuple[str, ...]
    max_body_chars: int
    max_attachment_bytes: int
    # --- calendar -------------------------------------------------------------
    caldav_url: str
    caldav_username: str
    caldav_require_tls: bool
    default_timezone: str
    default_calendar: str         # calendar new events go to when none is named ('' = auto)
    # --- feature switches -----------------------------------------------------
    enable_mail: bool
    enable_calendar: bool
    enable_contacts: bool
    enable_reminders: bool        # Reminders via the Mac helper (opt-in; needs BRIDGE_TOKEN)
    enable_notes: bool            # Notes via the Mac helper (opt-in; needs BRIDGE_TOKEN)
    enable_drive: bool            # iCloud Drive via the Mac helper (opt-in; needs BRIDGE_TOKEN)
    bridge_token: str             # shared secret of the Mac helper
    bridge_port: int              # private HTTPS port the Mac helper polls (never served on the public port)
    bridge_tls_names: tuple[str, ...]
    bridge_job_timeout: int       # seconds to wait for the Mac before a tool call fails
    carddav_url: str
    carddav_username: str
    read_only: bool
    allow_send: bool
    require_approval: bool        # outgoing mail is queued until the owner approves it on /outbox
    outbox_ttl: int               # seconds a queued message stays approvable
    outbox_max: int               # max messages waiting for approval
    allow_calendar_invites: bool  # calendar writes that make iCloud email other people (attendees)
    # --- MCP server / OAuth ---------------------------------------------------
    public_url: str
    owner_password: str
    data_dir: str
    host: str
    port: int
    stateless_http: bool          # no server-side MCP sessions: a restart never invalidates a client's connection
    tool_timeout: int             # seconds before a tool call is abandoned with an error instead of hanging
    allowed_redirect_hosts: tuple[str, ...]
    access_token_ttl: int
    refresh_token_ttl: int
    bridge_host: str = "127.0.0.1"  # address the bridge port binds to inside this process (0.0.0.0 only inside Docker, set by the image)
    local_mode: bool = False      # stdio for a desktop client on this computer: no OAuth, no public URL, no browser outbox
    tools: tuple[str, ...] = ()   # TOOLS: 'essential' and/or tool names to expose; empty = every tool of the enabled areas
    imap_pool_size: int = 3       # IMAP_POOL_SIZE: logged-in IMAP connections kept for reuse (0 = log in for every call)
    imap_idle_seconds: int = 600  # IMAP_IDLE_SECONDS: a pooled connection unused for longer is closed instead of reused
    caldav_pool_size: int = 4     # CALDAV_POOL_SIZE: CalDAV connections kept for reuse (calendars are read in parallel)
    caldav_keepalive_seconds: int = 600   # CALDAV_KEEPALIVE_SECONDS: keep pooled CalDAV connections warm this long after the last call (0 = off)
    warmup_on_start: bool = True  # WARMUP_ON_START: log in to mail, calendar and contacts in the background right after start
    tool_workers: int = 8         # TOOL_WORKERS: threads that run tool calls, so parallel calls do not queue behind each other
    shortcuts_allow: tuple[str, ...] = ()   # SHORTCUTS_ALLOW: exact Shortcut names the assistant may run (the Mac keeps its own list too)

    @classmethod
    def from_env(cls) -> "Settings":
        username = _str("ICLOUD_USERNAME")
        read_only = _bool("READ_ONLY", False)
        return cls(
            username=username,
            app_password=(_str("ICLOUD_APP_PASSWORD") or keychain_password(username, _str("ICLOUD_KEYCHAIN_SERVICE", "icloud-mcp"))).replace(" ", ""),
            email_address=_str("ICLOUD_EMAIL_ADDRESS", username),
            display_name=_str("ICLOUD_DISPLAY_NAME"),
            signature=_str("EMAIL_SIGNATURE").replace("\\n", "\n"),
            imap_host=_str("IMAP_HOST", "imap.mail.me.com"),
            imap_port=_int("IMAP_PORT", 993),
            imap_security=_str("IMAP_SECURITY", "ssl").lower(),
            imap_username=_str("IMAP_USERNAME", username),
            smtp_host=_str("SMTP_HOST", "smtp.mail.me.com"),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_security=_str("SMTP_SECURITY", "starttls").lower(),
            smtp_username=_str("SMTP_USERNAME", username),
            save_sent_copy=_bool("SAVE_SENT_COPY", True),
            allow_permanent_delete=_bool("ALLOW_PERMANENT_DELETE", False),
            max_recipients=_int("MAX_RECIPIENTS", 25),
            send_allowlist=tuple(x.lower() for x in _list("SEND_ALLOWLIST")),
            max_body_chars=_int("MAX_BODY_CHARS", 30000),
            max_attachment_bytes=_int("MAX_ATTACHMENT_BYTES", 5 * 1024 * 1024),
            caldav_url=_str("CALDAV_URL", "https://caldav.icloud.com"),
            caldav_username=_str("CALDAV_USERNAME", username),
            caldav_require_tls=_bool("CALDAV_REQUIRE_TLS", True),
            default_timezone=_str("DEFAULT_TIMEZONE", "UTC"),
            default_calendar=_str("DEFAULT_CALENDAR"),
            enable_mail=_bool("ENABLE_MAIL", True),
            enable_calendar=_bool("ENABLE_CALENDAR", True),
            enable_contacts=_bool("ENABLE_CONTACTS", True),
            enable_reminders=_bool("ENABLE_REMINDERS", False),
            enable_notes=_bool("ENABLE_NOTES", False),
            enable_drive=_bool("ENABLE_DRIVE", False),
            bridge_token=_str("BRIDGE_TOKEN"),
            bridge_port=_int("BRIDGE_PORT", 8001),
            bridge_tls_names=tuple(_list("BRIDGE_TLS_NAMES")),
            bridge_job_timeout=_int("BRIDGE_JOB_TIMEOUT_SECONDS", 60),
            carddav_url=_str("CARDDAV_URL", "https://contacts.icloud.com"),
            carddav_username=_str("CARDDAV_USERNAME", username),
            read_only=read_only,
            allow_send=_bool("ALLOW_SEND", True) and not read_only,   # READ_ONLY means no sending and no drafts either
            require_approval=_bool("SEND_REQUIRES_APPROVAL", True),
            outbox_ttl=_int("OUTBOX_TTL_SECONDS", 24 * 3600),
            outbox_max=_int("OUTBOX_MAX", 20),
            allow_calendar_invites=_bool("ALLOW_CALENDAR_INVITES", False),
            public_url=_str("MCP_PUBLIC_URL").rstrip("/"),
            owner_password=_str("MCP_OWNER_PASSWORD"),
            data_dir=_str("DATA_DIR", "./data"),
            host=_str("MCP_HOST", "127.0.0.1"),
            port=_int("MCP_PORT", 8000),
            stateless_http=_bool("MCP_STATELESS", True),
            tool_timeout=_int("TOOL_TIMEOUT_SECONDS", 60),
            allowed_redirect_hosts=tuple(
                h.lower() for h in _list("OAUTH_ALLOWED_REDIRECT_HOSTS", "claude.ai,claude.com,localhost,127.0.0.1")
            ),
            access_token_ttl=_int("ACCESS_TOKEN_TTL", 3600),
            refresh_token_ttl=_int("REFRESH_TOKEN_TTL", 60 * 60 * 24 * 30),
            bridge_host=_str("BRIDGE_HOST", "127.0.0.1"),
            tools=tuple(_list("TOOLS")),
            imap_pool_size=max(0, min(_int("IMAP_POOL_SIZE", 3), 8)),
            imap_idle_seconds=max(30, _int("IMAP_IDLE_SECONDS", 600)),
            caldav_pool_size=max(1, min(_int("CALDAV_POOL_SIZE", 4), 8)),
            caldav_keepalive_seconds=max(0, _int("CALDAV_KEEPALIVE_SECONDS", 600)),
            warmup_on_start=_bool("WARMUP_ON_START", True),
            tool_workers=max(2, min(_int("TOOL_WORKERS", 8), 32)),
            shortcuts_allow=tuple(n.strip() for n in _str("SHORTCUTS_ALLOW").split(";" if ";" in _str("SHORTCUTS_ALLOW") else ",") if n.strip()),
        )

    # ------------------------------------------------------------------
    @property
    def bridge_enabled(self) -> bool:
        return self.enable_reminders or self.enable_notes or self.enable_drive or bool(self.shortcuts_allow)

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc

    def validate_for_mail_calendar(self) -> None:
        missing = [n for n, v in (("ICLOUD_USERNAME", self.username), ("ICLOUD_APP_PASSWORD", self.app_password)) if not v]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

    def _refuse_placeholders(self, checks: list[tuple[str, bool]]) -> None:
        stale = [name for name, is_placeholder in checks if is_placeholder]
        if stale:
            raise SystemExit(f"{', '.join(stale)} still has the placeholder value from .env.example. Set your own value.")

    def _validate_bridge_and_areas(self) -> None:
        if self.bridge_enabled:
            if len(self.bridge_token) < 32 or "change-me" in self.bridge_token.lower():
                raise SystemExit("ENABLE_REMINDERS / ENABLE_NOTES / ENABLE_DRIVE / SHORTCUTS_ALLOW need BRIDGE_TOKEN: a random secret of at least 32 characters "
                                 "(for example `python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"`).")
            if self.owner_password and self.bridge_token == self.owner_password:
                raise SystemExit("BRIDGE_TOKEN must differ from MCP_OWNER_PASSWORD.")
        if not (self.enable_mail or self.enable_calendar or self.enable_contacts):
            raise SystemExit("ENABLE_MAIL, ENABLE_CALENDAR and ENABLE_CONTACTS are all false; nothing to serve.")

    def validate_for_local(self) -> None:
        """Local (stdio) mode: the desktop client on this computer starts the server itself, so there is no public URL or owner password."""
        self.validate_for_mail_calendar()
        self._refuse_placeholders([
            ("ICLOUD_APP_PASSWORD", "xxxx-xxxx" in self.app_password.lower()),
            ("ICLOUD_USERNAME", self.username.lower() == "you@icloud.com"),
        ])
        self._validate_bridge_and_areas()

    def validate_for_server(self) -> None:
        self.validate_for_mail_calendar()
        # Refuse to run with the untouched values from .env.example: a public mailbox server must not start with a guessable password.
        placeholders = [
            ("MCP_OWNER_PASSWORD", "change-me" in self.owner_password.lower()),
            ("ICLOUD_APP_PASSWORD", "xxxx-xxxx" in self.app_password.lower()),
            ("ICLOUD_USERNAME", self.username.lower() == "you@icloud.com"),
            ("MCP_PUBLIC_URL", "icloud-mcp.example.com" in self.public_url.lower()),
        ]
        self._refuse_placeholders(placeholders)
        if not self.public_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise SystemExit("MCP_PUBLIC_URL must be the public https:// URL of this server (no trailing path).")
        if len(self.owner_password) < 12:
            raise SystemExit("MCP_OWNER_PASSWORD must be set and at least 12 characters long.")
        self._validate_bridge_and_areas()
