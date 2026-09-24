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

import re
from typing import Any

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
    visible = clean(joined)
    out += [msg for pattern, msg in _WARNINGS if pattern.search(visible)]
    return out
