"""Identity keys: name normalization, position grouping, person keys."""

import re
import unicodedata

# Letters with no NFKD combining-mark decomposition (not simple accented forms).
_TRANSLITERATE = {
    "ø": "o", "Ø": "O",  # o/O with stroke
    "æ": "ae", "Æ": "AE",  # ae ligature
    "ł": "l", "Ł": "L",  # l with stroke
    "đ": "d", "Đ": "D",  # d with stroke
    "ß": "ss",  # sharp s
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")

_FORWARD_POSITIONS = {"C", "LW", "RW", "W"}


def normalize_name(s: str) -> str:
    for src, dst in _TRANSLITERATE.items():
        s = s.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    # Punctuation (hyphens, apostrophes, periods) becomes a word boundary, not glue,
    # so "Aston-Reese" normalizes to "aston reese", not "astonreese".
    spaced = _NON_ALNUM.sub(" ", lowered)
    return _WHITESPACE.sub(" ", spaced).strip()


def position_group(pos: str) -> str:
    p = pos.strip().upper()
    if p in _FORWARD_POSITIONS:
        return "F"
    if p == "D":
        return "D"
    if p == "G":
        return "G"
    raise ValueError(f"unrecognized position: {pos!r}")


def person_key(name: str, team: str, position: str) -> str:
    return f"{normalize_name(name)}|{team}|{position_group(position)}"
