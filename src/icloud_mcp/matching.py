"""Forgiving name matching shared by contacts and mail lookups.

People misspell names ("Katryn" for "Katrien", "Stefan" for "Stephan"), so exact substring search alone answers "no match" when the person
is right there. These helpers give a sound-alike key plus a similarity score. Anything found only through them is an *approximate*
match: callers must present it as "did you mean...?" and never act on it without the user confirming.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_LATIN = re.compile(r"^[a-z]+$")
SIMILAR_MIN = 0.8          # similarity needed for words of 4+ letters (shorter words must sound identical)


def norm(s: str) -> str:
    """Case- and accent-insensitive form; keeps non-Latin scripts intact."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)).casefold()


def phonetic_key(word: str) -> str:
    """Rough sound-alike key for Latin-script names (English/Dutch/German-ish spellings). Non-Latin words are returned as-is."""
    w = norm(word)
    if not _LATIN.match(w):
        return w
    for src, dst in (("ij", "i"), ("sch", "sk"), ("ph", "f"), ("ck", "k"), ("th", "t"), ("q", "k"), ("x", "ks"), ("z", "s"), ("w", "v")):
        w = w.replace(src, dst)
    w = re.sub(r"c(?=[aou]|$)", "k", w)
    w = w.replace("c", "s")
    w = re.sub(r"ie|ei|ey|ay|y", "i", w)
    w = w[:1] + w[1:].replace("h", "")
    return re.sub(r"(.)\1+", r"\1", w)


def word_similarity(a: str, b: str) -> float:
    """0..1 similarity between two words: the better of spelling similarity and sound-alike similarity."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    spelling = SequenceMatcher(None, na, nb).ratio()
    sound = SequenceMatcher(None, phonetic_key(na), phonetic_key(nb)).ratio()
    return max(spelling, sound)


def similar_enough(query_word: str, candidate_word: str) -> float:
    """Similarity if the two words plausibly are the same name, else 0. Short words must sound identical."""
    q, c = norm(query_word), norm(candidate_word)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if min(len(q), len(c)) <= 3:
        return 1.0 if phonetic_key(q) == phonetic_key(c) and _LATIN.match(q) and _LATIN.match(c) else 0.0
    sim = word_similarity(q, c)
    return sim if sim >= SIMILAR_MIN else 0.0


def fuzzy_match_all(query_tokens: list[str], words: list[str]) -> float:
    """Mean best similarity if EVERY query token plausibly matches some word; 0 if any token matches none."""
    if not query_tokens or not words:
        return 0.0
    total = 0.0
    for tok in query_tokens:
        best = max((similar_enough(tok, w) for w in words), default=0.0)
        if best == 0.0:
            return 0.0
        total += best
    return total / len(query_tokens)
