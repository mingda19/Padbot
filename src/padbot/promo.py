"""Multi-buy promo maths ("2 FOR $9.90", "MIN 3 GET 33% OFF").

Watsons exposes the headline promo as a short label. Only labels that reduce
the price of the pad itself are understood; basket promos ("$50 OFF $340"),
loyalty points ("3X BONUS POINTS") and flat markdowns ("SAVE 20%", already in
`price`) are ignored. Unrecognised labels return None, and the bot falls back
to showing the original price struck through.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_N_FOR_PRICE = re.compile(r"^(\d+)\s*for\s*\$\s*(\d+(?:\.\d+)?)$", re.I)
_N_FOR_PCT = re.compile(r"^(\d+)\s*for\s*(\d+(?:\.\d+)?)\s*%\s*off$", re.I)
_MIN_N_GET_PCT = re.compile(r"^min\.?\s*(\d+)\s*get\s*(\d+(?:\.\d+)?)\s*%\s*off$", re.I)


@dataclass(frozen=True)
class MultiBuy:
    text: str
    qty: int  # units to buy
    total: float  # price for all `qty` units


def _cents(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _one(text: str, unit_price: Decimal) -> MultiBuy | None:
    text = text.strip()
    if m := _N_FOR_PRICE.match(text):
        qty, total = int(m[1]), Decimal(m[2])
    elif m := (_N_FOR_PCT.match(text) or _MIN_N_GET_PCT.match(text)):
        qty = int(m[1])
        total = unit_price * qty * (1 - Decimal(m[2]) / 100)
    else:
        return None
    if qty < 2:
        return None
    return MultiBuy(text, qty, _cents(total))


def parse_multibuy(labels: Iterable[str | None], unit_price: float) -> MultiBuy | None:
    """Best multi-buy among `labels`, or None if none beats buying singly."""
    unit = Decimal(str(unit_price))
    best: MultiBuy | None = None
    for label in labels:
        if not label:
            continue
        deal = _one(label, unit)
        if deal is None or Decimal(str(deal.total)) >= unit * deal.qty:
            continue  # unparseable, or not actually cheaper
        if best is None or deal.total / deal.qty < best.total / best.qty:
            best = deal
    return best
