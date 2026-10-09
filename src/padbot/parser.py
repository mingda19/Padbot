"""Turn a Watsons product name into (pad_count, length_cm).

Patterns come from ~215 real names collected in the spike. Anything ambiguous
fails with a reason rather than guessing: a wrong price per pad ranked first is
worse than a missing row.
"""

import html
import re
from dataclasses import dataclass

from .config import MAX_LENGTH_CM, MIN_LENGTH_CM

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def clean_name(raw: str) -> str:
    """Names contain stray markup ("... 16s<BR>") and curly apostrophes."""
    text = _TAGS.sub(" ", html.unescape(raw))
    text = text.replace("’", "'").replace("‘", "'").replace("´", "'")
    return _SPACES.sub(" ", text).strip()


# The plan's list plus what the spike found sharing these categories: period
# panties ("Overnight Panties", "Night Panty 360"), reusable cloth pads, and
# incontinence pads. "liner" also covers pantyliner / "Pantiliner".
_NOT_PAD = re.compile(
    r"liner|tampon|\bcups?\b|\bpants\b|\bpant(?:y|ies|ie)\b|underwear"
    r"|cloth pad|reusable|washable|incontinence",
    re.I,
)


def is_pad(name: str) -> bool:
    return not _NOT_PAD.search(name)


# --- length ---------------------------------------------------------------

# "(?<![\d.])" stops "0.07cm" (thickness) being read as "07cm". The optional
# leading "24-" flags ranges like "24-28cm", which we refuse to guess at.
_LENGTH = re.compile(
    r"(?:(?P<lo>\d{1,2}(?:\.\d+)?)\s*[-–]\s*)?"
    r"(?<![\d.])(?P<v>\d{1,3}(?:\.\d+)?)\s*(?P<unit>cm|mm)\b",
    re.I,
)


def parse_length(name: str) -> tuple[float | None, str | None]:
    """cm or mm ("Young Girl 280mm"); implausible values such as thickness are
    ignored. Returns (length_cm, failure_reason)."""
    found: set[float] = set()
    for m in _LENGTH.finditer(name):
        value = float(m["v"])
        if m["unit"].lower() == "mm":
            value /= 10
        if not MIN_LENGTH_CM <= value <= MAX_LENGTH_CM:
            continue
        if m["lo"] is not None:
            return None, "ambiguous_length"
        found.add(round(value, 1))
    if not found:
        return None, "no_length"
    if len(found) > 1:
        return None, "ambiguous_length"
    return found.pop(), None


# --- count ----------------------------------------------------------------

_UNIT = r"(?:'?s|pcs?|pieces?|pads?|counts?)"
# "14s x 2", "16sx2", "8's x 2", "9s x 2s" -> count x packs
_MULTI_COUNT_FIRST = re.compile(rf"(?<![\d.])(\d+)\s*{_UNIT}\s*[x×]\s*(\d+)(?!\d)", re.I)
# "2 x 14s" -> packs x count
_MULTI_PACKS_FIRST = re.compile(rf"(?<![\d.])(\d+)\s*[x×]\s*(\d+)\s*{_UNIT}\b", re.I)
_PLAIN = re.compile(rf"(?<![\d.])(\d+)\s*{_UNIT}\b", re.I)

# Combos, and bundles with a freebie: "(16s x 2) + 2s", "10s + 2s", "day + night".
_MIXED = re.compile(
    r"\bcombo\b|\bassorted\b|\bmixed\b"
    r"|\d\s*(?:'?s|pcs|pieces)?\s*\)?\s*\+\s*(?:free\s+)?\d+\s*(?:'?s|pcs|pieces)\b"
    r"|\b(?:day|night)\s*(?:\+|&|and)\s*(?:day|night)\b",
    re.I,
)


def parse_count(text: str) -> tuple[int | None, str | None]:
    """Total pads in `text`, multiplying multipacks. (None, None) = no count."""
    totals: list[int] = []
    for rx in (_MULTI_COUNT_FIRST, _MULTI_PACKS_FIRST):

        def take(m: re.Match) -> str:
            totals.append(int(m[1]) * int(m[2]))
            return " " * len(m[0])  # blank it so _PLAIN can't re-read "2s"

        text = rx.sub(take, text)
    totals += [int(m[1]) for m in _PLAIN.finditer(text)]
    distinct = {t for t in totals if t > 0}
    if not distinct:
        return None, None
    if len(distinct) > 1:
        return None, "ambiguous_count"
    return distinct.pop(), None


# --- whole product --------------------------------------------------------


@dataclass(frozen=True)
class ParseResult:
    is_pad: bool
    pad_count: int | None = None
    length_cm: float | None = None
    reason: str | None = None  # why parse_ok would be 0

    @property
    def ok(self) -> bool:
        return self.is_pad and self.reason is None


def parse_product(raw_name: str, content_size_unit: str | None = None) -> ParseResult:
    """`content_size_unit` is Watsons' structured pack size ("16s", "8s x 2").

    The name is authoritative. The structured field is only a fallback when the
    name has no count (e.g. "Hadaomoi Day Slim Wing 23cm" / "20S"), and a
    cross-check otherwise: it disagrees with the name on a few SKUs ("17s" vs
    "15S") and we can't tell which is right, so those are rejected.
    """
    name = clean_name(raw_name)
    if not is_pad(name):
        return ParseResult(is_pad=False)

    length, length_reason = parse_length(name)
    size = clean_name(content_size_unit) if content_size_unit else ""

    if _MIXED.search(name) or "+" in size:
        return ParseResult(True, None, length, "mixed_pack")

    name_count, name_reason = parse_count(name)
    if name_reason:
        return ParseResult(True, None, length, name_reason)
    size_count, _ = parse_count(size) if size else (None, None)

    if name_count and size_count and name_count != size_count:
        return ParseResult(True, None, length, "count_conflict")
    count = name_count or size_count
    if count is None:
        return ParseResult(True, None, length, "no_count")
    return ParseResult(True, count, length, length_reason)
