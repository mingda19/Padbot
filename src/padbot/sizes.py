"""Size buckets and the Custom length input."""

import re
from dataclasses import dataclass

from .config import MAX_LENGTH_CM, MIN_LENGTH_CM


class SizeInputError(ValueError):
    """User-facing: the message is shown to the user as is."""


@dataclass(frozen=True)
class SizeRange:
    min_cm: float
    max_cm: float
    max_inclusive: bool
    label: str
    token: str  # what goes in callback_data, so "Show more" can rebuild the query


# Half-open, so a 24cm pad lands in exactly one bucket.
BUCKETS = {
    "light": SizeRange(16, 24, False, "Light 16–24cm", "light"),
    "medium": SizeRange(24, 35, False, "Medium 24–35cm", "medium"),
    "heavy": SizeRange(35, 1000, False, "Heavy 35cm+", "heavy"),
}

_NUM = r"(\d+(?:\.\d+)?)"
# "28", "28cm", "28-32", "28 - 32", "28–32cm", "28 to 32"
_INPUT = re.compile(rf"^\s*{_NUM}\s*(?:cm)?\s*(?:(?:-|–|—|~|to)\s*{_NUM}\s*(?:cm)?)?\s*$", re.I)
_TOKEN = re.compile(rf"^c{_NUM}-{_NUM}$")

HELP = f"Send a length like 28, or a range like 28-32 ({MIN_LENGTH_CM:g}–{MAX_LENGTH_CM:g} cm)."


def _custom(lo: float, hi: float, label: str) -> SizeRange:
    return SizeRange(lo, hi, True, label, f"c{lo:g}-{hi:g}")


def parse_length_input(text: str) -> SizeRange:
    """Custom is inclusive at both ends. A single length means that length give
    or take 0.5cm, since pads are listed at 23, 23.5, 24, ... and an exact match
    on "28" would hide 28.5."""
    m = _INPUT.match(text or "")
    if not m:
        raise SizeInputError(f"I couldn't read that. {HELP}")
    first = float(m[1])
    second = float(m[2]) if m[2] is not None else None
    for value in (first, second):
        if value is not None and not MIN_LENGTH_CM <= value <= MAX_LENGTH_CM:
            raise SizeInputError(f"{value:g}cm is outside {MIN_LENGTH_CM:g}–{MAX_LENGTH_CM:g}cm. {HELP}")
    if second is None:
        return _custom(first - 0.5, first + 0.5, f"around {first:g}cm (±0.5)")
    lo, hi = sorted((first, second))
    return _custom(lo, hi, f"{lo:g}–{hi:g}cm")


def size_from_token(token: str) -> SizeRange | None:
    if token in BUCKETS:
        return BUCKETS[token]
    if m := _TOKEN.match(token):
        lo, hi = float(m[1]), float(m[2])
        label = f"{lo:g}–{hi:g}cm"
        return _custom(lo, hi, label)
    return None
