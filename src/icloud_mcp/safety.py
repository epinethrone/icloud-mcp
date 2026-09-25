"""Defences for text written by other people (mail, invitations, notes, files) before it reaches a model.

Two things, both cheap and deliberately conservative:

- Invisible characters a reader cannot see are removed: Unicode tag characters (which can smuggle whole sentences),
  zero-width spaces and invisible operators, and the direction overrides/isolates that make text display in a different
  order than a model reads it. Left-to-right/right-to-left marks and zero-width (non-)joiners stay, because Kurdish,
  Persian and Arabic text and emoji sequences rely on them.
- Text that looks like an attempt to steer the agent, or asks to change bank or payment details, gets a plain-language
  warning next to it. The warning is a hint for the model and the owner, not a filter: nothing is removed or blocked.
"""
from __future__ import annotations

import hashlib as _hashlib
import hmac as _hmac
import json as _json
import logging
import re
import secrets as _secrets
import subprocess as _subprocess
import threading as _threading
import time as _time
from collections import Counter
from typing import Any

log = logging.getLogger("icloud_mcp.safety")

# Monitoring: how often each warning fired since the server started (pattern ids only, never text). Read by icloud_check_health.
STATS: Counter = Counter()
_STATS_LOCK = _threading.Lock()
_SCREEN_COMMAND: str | None = None   # set by configure_screen(); None = built-in patterns only

_INVISIBLE = re.compile(
    "[­᠎​‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]"
)

_WARNINGS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|your)\b.{0,25}"
                r"\b(instructions?|prompts?|rules|guidelines)\b", re.I | re.S),
     "It contains text telling an AI to ignore its instructions."),
    (re.compile(r"(\bsystem prompt\b|\byou are now\b|\bnew instructions\b|\bdeveloper mode\b|^\s*(system|assistant)\s*:)",
                re.I | re.M),
     "It contains text addressed to an AI assistant rather than to a person."),
    (re.compile(r"\b(send|forward|email|mail|share|reveal|give)\b.{0,60}\b(passwords?|credentials|api keys?|tokens?|"
                r"verification codes?|2fa codes?|one[- ]time codes?|all (?:your|the|his|her) (?:emails|messages|contacts))\b",
                re.I | re.S),
     "It asks for passwords, codes or bulk data to be sent somewhere."),
    (re.compile(r"\b(new|updated|changed|different|correct)\b.{0,40}\b(bank(?:ing)? (?:account|details)|account number|iban|"
                r"payment details|wire instructions)\b", re.I | re.S),
     "It says bank or payment details have changed. That is the classic invoice-fraud pattern: confirm by phone first."),
    (re.compile(r"(\b(nieuwe?|gewijzigde?|andere?|juiste)\b.{0,40}\b(rekeningnummer|bankrekening(?:nummer)?|iban|bankgegevens)\b"
                r"|\b(rekeningnummer|bankrekening(?:nummer)?|iban|bankgegevens)\b.{0,40}\b(gewijzigd|veranderd|aangepast|nieuw)\b)",
                re.I | re.S),
     "It says bank or payment details have changed (Dutch). That is the classic invoice-fraud pattern: confirm by phone first."),
]


def clean(value: str) -> str:
    """The text with invisible steering characters removed."""
    return _INVISIBLE.sub("", value)


# ------------------------------------------------------------------------------------------- text a human reader cannot see
# HTML mail can carry text that never renders: hidden elements, zero-size fonts, HTML comments. A reader does not see it, a model
# converting the HTML to text does, which is exactly where instructions get hidden. Those parts are removed before conversion;
# colour tricks (white on white) cannot be judged without rendering and are left in place.
_HIDDEN_STYLE = r"(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*[;\"'])"
_HIDDEN_ELEMENT = re.compile(
    r"<(?P<tag>[a-zA-Z][\w-]*)(?=[^>]*(?:\bhidden\b(?=[\s>/=])|style\s*=\s*[\"'][^\"']*" + _HIDDEN_STYLE + r"))[^>]*>.*?</(?P=tag)\s*>",
    re.I | re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def strip_hidden_html(html: str) -> tuple[str, bool]:
    """(html without elements a reader cannot see, whether anything was removed). Bounded: a document over 2 MB is left as is,
    because the regexes scan it whole."""
    if not html or len(html) > 2_000_000:
        return html, False
    out, n1 = _HTML_COMMENT.subn("", html)
    out, n2 = _HIDDEN_ELEMENT.subn("", out)
    return out, bool(n1 or n2)


HIDDEN_TEXT_WARNING = "It contained text hidden from a human reader (removed before conversion); that is how instructions are smuggled past the person."


def configure_screen(setting: str) -> None:
    """SAFETY_SCREEN: '' keeps the built-in patterns; 'command:<path>' adds the owner's own classifier, a program that reads the
    text on stdin and prints JSON with a boolean 'injection_suspected'. Nothing leaves the server unless the owner's program
    sends it somewhere, which is their choice and their configuration."""
    global _SCREEN_COMMAND
    setting = (setting or "").strip()
    if not setting:
        _SCREEN_COMMAND = None
    elif setting.startswith("command:") and setting[8:].strip():
        _SCREEN_COMMAND = setting[8:].strip()
    else:
        raise SystemExit("SAFETY_SCREEN must be empty or 'command:<path to a program>'.")


SCREEN_WARNING = "The owner's screening program judged this text a likely attempt to steer an AI."
SCREEN_FAILED_WARNING = "The owner's screening program could not judge this text (it failed or timed out)."


def _run_screen(text: str) -> str | None:
    if _SCREEN_COMMAND is None:
        return None
    try:
        r = _subprocess.run([_SCREEN_COMMAND], input=text[:200_000].encode("utf-8", "replace"), capture_output=True, timeout=8)
        verdict = _json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        if r.returncode != 0 or not isinstance(verdict, dict) or not isinstance(verdict.get("injection_suspected"), bool):
            raise ValueError("bad verdict")
    except (OSError, ValueError, _subprocess.TimeoutExpired) as e:
        log.warning("safety screen failed: %s", type(e).__name__)
        return SCREEN_FAILED_WARNING
    return SCREEN_WARNING if verdict["injection_suspected"] else None


def _count(key: str) -> None:
    with _STATS_LOCK:
        STATS[key] += 1
    log.info("safety: warning %s", key)          # the id only: the text and its sender stay out of the log


def stats() -> dict[str, int]:
    with _STATS_LOCK:
        return dict(STATS)


_BASE64 = re.compile(r"[A-Za-z0-9+/=\r\n]*")
_BIG = 256 * 1024


def clean_deep(value: Any) -> Any:
    """clean() applied to every string inside a tool result (dicts, lists, tuples). A very large string that is pure base64
    (an attachment or a file) is passed through as is: it cannot carry invisible characters, and walking it costs time."""
    if isinstance(value, str):
        if len(value) > _BIG and _BASE64.fullmatch(value):
            return value
        return clean(value)
    if isinstance(value, dict):
        return {k: clean_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(clean_deep(v) for v in value)
    return value


def compact(d: dict[str, Any], keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """d without keys whose value is empty (None, "", [], {}, False), except the keys in `keep`. For list results, where the
    same empty fields repeated on every item were a large share of the bytes; a missing field means "none"."""
    return {k: v for k, v in d.items() if k in keep or v not in (None, "", [], {}, False)}


def warnings_for(*texts: str | None) -> list[str]:
    """Plain-language warnings for untrusted text; an empty list when nothing looks off."""
    joined = "\n".join(t for t in texts if t)
    if not joined:
        return []
    out = []
    if _INVISIBLE.search(joined):
        out.append("It contained hidden characters (removed), which is how instructions are smuggled past a reader.")
        _count("invisible_characters")
    visible = clean(joined)
    for i, (pattern, msg) in enumerate(_WARNINGS):
        if pattern.search(visible):
            out.append(msg)
            _count(f"pattern_{i}")
    if (screened := _run_screen(visible)) is not None:
        out.append(screened)
        _count("screen_flagged" if screened == SCREEN_WARNING else "screen_failed")
    return out


# ---------------------------------------------------------------------------------------------------- confirm tokens
# A destructive step that cannot be undone from here (deleting a non-empty folder, calendar or list) runs in two calls: the
# first previews what would go and returns a token that stands for exactly that state; the second needs the token. The key
# is per process, so a token can only come from a preview, and it expires.
_TOKEN_KEY = _secrets.token_bytes(32)
CONFIRM_TTL = 600


def confirm_token(kind: str, *state: Any, issued: int | None = None) -> str:
    issued = int(_time.time()) if issued is None else issued
    raw = "\x1f".join([kind, str(issued), *(str(p) for p in state)])
    return f"{issued}.{_hmac.new(_TOKEN_KEY, raw.encode(), _hashlib.sha256).hexdigest()[:20]}"


def confirm_problem(token: str | None, kind: str, *state: Any, ttl: int = CONFIRM_TTL) -> str | None:
    """Why a token is not accepted for this state, or None when it is valid."""
    issued_s, _, _ = (token or "").partition(".")
    if not issued_s.isdigit():
        return "confirm_token is not one this server issued: call again without it to see the preview and get a token."
    if not _hmac.compare_digest(token or "", confirm_token(kind, *state, issued=int(issued_s))):
        return "confirm_token does not match what is there now (it changed since the preview): call again without it for a new preview."
    if _time.time() - int(issued_s) > ttl:
        return f"confirm_token is older than {ttl // 60} minutes: call again without it for a new preview."
    return None
