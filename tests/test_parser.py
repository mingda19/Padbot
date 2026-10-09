import pytest

from padbot.parser import clean_name, is_pad, parse_length, parse_product

# (name, contentSizeUnit, pad_count, length_cm). Real names from the spike
# unless marked "synthetic" in the failure cases below.
PARSES = [
    ("Comfort V-Gentle Slim Soft Wings 36cm for Night Value Pack 8s x 2", "8s x 2", 16, 36.0),
    ("Comfort V-Gentle Ultra Thin Wings 23.5cm for Day 24s", "24s", 24, 23.5),
    ("Odour Control Cranberry Extract Super Ultra Thin Wing Pad 42cm 5s x 2", "5s x 2", 10, 42.0),
    ("Crown Slim Pad 0.07cm 24cm 16s", "16s", 16, 24.0),  # thickness is not length
    ("Maxi Overnight & Maternity Sanitary Pads 41cm x 8s", "8s", 8, 41.0),
    ("Regular Flow Sanitary Pads 24cm x 12s", "12s", 12, 24.0),
    ("Cicada B5 Wing 28cm 13s", "13pcs", 13, 28.0),
    ("Luxe Ultra Thin Wing 28cm Twin Pack 14s x 2", "28pcs", 28, 28.0),
    ("Body Fit Night Slim Wing 35cm 16sx2 Twin Pack", "16's x 2", 32, 35.0),
    ("Body Fit Night Slim Wing 42cm Twin Pack 8's x 2", "8's x 2", 16, 42.0),
    ("Cooling Fresh Night Slim Wing 35cm Twin Pack 9s x 2s (Expiry: Jul`2027)", "18s", 18, 35.0),
    ("Super Slimguard Day Wing Sanitary Pad 30cm Twin Packset 14s x 2 Pack", "28s", 28, 30.0),
    ("Super Slimguard Night Wing Sanitary Pad 30cm Twin Packset 14s x 2", "1 set", 28, 30.0),
    ("Super Slimguard Day 25cm Sanitary Napkin 16s x 2s", "16s x 2", 32, 25.0),
    ("Safety Comfort Day 22.5cm 16's Sanitary Napkins", "16S", 16, 22.5),
    ("SLIM COMFORT ULTRA SLIM 25CM 14S", "14S", 14, 25.0),
    ("Hadaomoi Day Slim Wing 26cm 16 Pieces", "PACK", 16, 26.0),
    ("Hadaomoi Day Slim Wing 23cm", "20S", 20, 23.0),  # no count in name: structured field
    ("Young Girl 280mm 12s", None, 12, 28.0),  # millimetres
    ("Laurier Fresh Protect Day Anti-Bacterial 22.5cm Ultra Slim Pads 16s<BR>", "16 pcs", 16, 22.5),
    (
        "Good Night Ultra Slim Maximum Assurance Sanitary Pad All Night 28cm (For Heavy Flow) 8s",
        "8 Pieces",
        8,
        28.0,
    ),
    (
        "Happy Days Ultra Slim Negative Ions Plus Sanitary Pad 24cm (For Daily use) 10s",
        "PIECE",
        10,
        24.0,
    ),
    ("Pad Super 27.6cm 14s", "14s", 14, 27.6),
    (
        "Super Overnight Herbal Slim Sanitary Pad 35cm 99.9% Anti-Bacterial Up to 10hrs "
        "Leakage Protection (For Heavy Flow) 12s",
        "12 Pieces",
        12,
        35.0,
    ),
    (
        "Comfort Nite Longest Anti-Bacterial Odor Care  Motion Fit Anti-Back Leakage "
        "Sanitary Pad Night Wing 42.5cm (For Heavy Flow) 8s",
        "8pcs",
        8,
        42.5,
    ),
]


@pytest.mark.parametrize("name,size,count,length", PARSES)
def test_parses(name, size, count, length):
    r = parse_product(name, size)
    assert r.ok, r.reason
    assert (r.pad_count, r.length_cm) == (count, length)


# (name, size, expected reason, count, length)
FAILURES = [
    # structured field and name disagree and we can't tell which is right
    ("Cool Technology Night 360mm 4s", "8S", "count_conflict", None, 36.0),
    ("Ultra Gentle Day Sanitary Pad 25cm Ultra Slim 17s", "15S", "count_conflict", None, 25.0),
    ("Hadaomoi Organic Night Slim Wing 36cm (Powerful Absorbency) 8s", "9s", "count_conflict", None, 36.0),
    # no length anywhere
    ("Petite Pads With Organic Cotton Cover 14s (Expiry: Mar`2027)", "14s", "no_length", 14, None),
    ("Premium Lady Pad 1.5 Drop 12s", "12s", "no_length", 12, None),
    # no count anywhere
    ("Hadaomoi Day Slim Wing 23cm", "PACK", "no_count", None, 23.0),
    # pad + freebie in one SKU
    ("Super Slimguard Day 22.5cm TwinPack With Mini Furry Bag (20s x 2) + 1s", "40s + 1s", "mixed_pack", None, 22.5),
    # synthetic
    ("Day 25cm 16s + Night 35cm 8s Combo Pack", None, "mixed_pack", None, None),
    ("Sanitary Pad 24cm and 29cm 12s", None, "ambiguous_length", 12, None),
    ("Sanitary Pad 24-28cm 12s", None, "ambiguous_length", 12, None),
    ("Sanitary Pad 25cm Day 16s Night 8s", None, "ambiguous_count", None, 25.0),
]


@pytest.mark.parametrize("name,size,reason,count,length", FAILURES)
def test_failures_are_flagged_not_guessed(name, size, reason, count, length):
    r = parse_product(name, size)
    assert r.is_pad and not r.ok
    assert (r.reason, r.pad_count, r.length_cm) == (reason, count, length)


NOT_PADS = [
    "Comfort & Freshness Ultra Thin Liners 15cm 40s",
    "Extra Dry Pantyliners 175mm 34's",
    "Pantiliner Long & Wide Fit Absorb (Unscented) 40s",
    "Hadaomoi Anti-Bac Panty Liner 62s",
    "Overnight Panties Herbal Anti-Bacterial Size S-M 2s",
    "Ergo Comfort Overnight Panty XL 3s",
    "Comfort 2 in 1 Night Panty 360 Size M-L (12hr Protection + Fit & Stretchable + Airy Breathable) 5s",
    'Dream V-Gentle Overnight Panties M-L (Hip 32" - 41" & Waist Up To 35") 2s',
    "Pearl Plastic Regular Absorbency Unscented Tampons 18s",
    "Organic Cotton Cloth Pad Super Ultra Koala (with Wings + Washable & Reusable) 1s",
    "TENA Discreet Ultra Mini Incontinence pad 26s",
    "Super Slimguard Day 25cm TwinPack With Free Night Guard Panties M-L (16s x 2) + 2s",
]


@pytest.mark.parametrize("name", NOT_PADS)
def test_non_pads_are_filtered(name):
    r = parse_product(name)
    assert not r.is_pad and not r.ok


@pytest.mark.parametrize(
    "name",
    [
        "Love Plus Large (L) 29cm Sanitary Pad 14s",
        "Laurier Fresh Protect Night Anti-Bacterial 30cm Ultra Slim Pads 12s<BR>",
        "Cooling Fresh Day Ultra Slim Wing 25cm 14s",
    ],
)
def test_real_pads_are_not_filtered(name):
    assert is_pad(clean_name(name))


def test_clean_name():
    assert clean_name("Pads 12s<BR>") == "Pads 12s"
    assert clean_name("Pads  &amp;  Wings’ 16’s") == "Pads & Wings' 16's"


def test_length_bounds():
    assert parse_length("Pad 5cm 12s") == (None, "no_length")
    assert parse_length("Pad 90cm 12s") == (None, "no_length")
    assert parse_length("Pad 10cm 12s") == (10.0, None)
