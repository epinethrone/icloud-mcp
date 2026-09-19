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

    @classmethod
    def from_env(cls) -> "Settings":
        username = _str("ICLOUD_USERNAME")
        return cls(
            username=username,
            app_password=_str("ICLOUD_APP_PASSWORD").replace(" ", ""),
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
            carddav_url=_str("CARDDAV_URL", "https://contacts.icloud.com"),
            carddav_username=_str("CARDDAV_USERNAME", username),
            read_only=_bool("READ_ONLY", False),
            allow_send=_bool("ALLOW_SEND", True),
            require_approval=_bool("SEND_REQUIRES_APPROVAL", True),
            outbox_ttl=_int("OUTBOX_TTL_SECONDS", 24 * 3600),
            outbox_max=_int("OUTBOX_MAX", 20),
            allow_calendar_invites=_bool("ALLOW_CALENDAR_INVITES", False),
            public_url=_str("MCP_PUBLIC_URL").rstrip("/"),
            owner_password=_str("MCP_OWNER_PASSWORD"),
            data_dir=_str("DATA_DIR", "./data"),
            host=_str("MCP_HOST", "0.0.0.0"),
            port=_int("MCP_PORT", 8000),
            stateless_http=_bool("MCP_STATELESS", True),
            tool_timeout=_int("TOOL_TIMEOUT_SECONDS", 90),
            allowed_redirect_hosts=tuple(
                h.lower() for h in _list("OAUTH_ALLOWED_REDIRECT_HOSTS", "claude.ai,claude.com,localhost,127.0.0.1")
            ),
            access_token_ttl=_int("ACCESS_TOKEN_TTL", 3600),
            refresh_token_ttl=_int("REFRESH_TOKEN_TTL", 60 * 60 * 24 * 30),
        )

    # ------------------------------------------------------------------
    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc

    def validate_for_mail_calendar(self) -> None:
        missing = [n for n, v in (("ICLOUD_USERNAME", self.username), ("ICLOUD_APP_PASSWORD", self.app_password)) if not v]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

    def validate_for_server(self) -> None:
        self.validate_for_mail_calendar()
        # Refuse to run with the untouched values from .env.example: a public mailbox server must not start with a guessable password.
        placeholders = [
            ("MCP_OWNER_PASSWORD", "change-me" in self.owner_password.lower()),
            ("ICLOUD_APP_PASSWORD", "xxxx-xxxx" in self.app_password.lower()),
            ("ICLOUD_USERNAME", self.username.lower() == "you@icloud.com"),
            ("MCP_PUBLIC_URL", "icloud-mcp.example.com" in self.public_url.lower()),
        ]
        stale = [name for name, is_placeholder in placeholders if is_placeholder]
        if stale:
            raise SystemExit(f"{', '.join(stale)} still has the placeholder value from .env.example. Set your own value in .env.")
        if not self.public_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise SystemExit("MCP_PUBLIC_URL must be the public https:// URL of this server (no trailing path).")
        if len(self.owner_password) < 12:
            raise SystemExit("MCP_OWNER_PASSWORD must be set and at least 12 characters long.")
        if not (self.enable_mail or self.enable_calendar or self.enable_contacts):
            raise SystemExit("ENABLE_MAIL, ENABLE_CALENDAR and ENABLE_CONTACTS are all false; nothing to serve.")
