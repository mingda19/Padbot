"""Regression run over every product from the spike (215 SKUs from Regular and
Overnight Napkins, scraped 2026-10-09). The exact numbers are pinned on purpose:
if a parser change moves them, look at which names flipped."""

import json
from pathlib import Path

from padbot.parser import parse_product

CATALOGUE = json.loads((Path(__file__).parent / "fixtures" / "spike_catalogue.json").read_text())


def test_spike_catalogue_parse_rate():
    results = [(p, parse_product(p["name"], p["size"])) for p in CATALOGUE]
    pads = [(p, r) for p, r in results if r.is_pad]
    ok = [(p, r) for p, r in pads if r.ok]
    assert (len(CATALOGUE), len(pads), len(ok)) == (215, 146, 134)

    reasons = sorted(r.reason for _, r in pads if not r.ok)
    assert reasons == ["count_conflict"] * 4 + ["mixed_pack"] * 2 + ["no_length"] * 6


def test_spike_catalogue_has_no_price_outliers():
    # A miscounted pack shows up as an implausible price per pad.
    for p in CATALOGUE:
        r = parse_product(p["name"], p["size"])
        if r.ok and p["stock"] == "inStock":
            assert 0.10 <= p["price"] / r.pad_count <= 1.50, p["name"]
