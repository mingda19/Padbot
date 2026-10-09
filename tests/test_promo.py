import pytest

from padbot.promo import MultiBuy, parse_multibuy


@pytest.mark.parametrize(
    "label,price,qty,total",
    [
        ("2 FOR $9.90", 6.2, 2, 9.90),
        ("2 for $11.30", 6.55, 2, 11.30),
        ("2 FOR $7", 3.9, 2, 7.00),
        ("3 FOR $13.95", 6.25, 3, 13.95),
        ("3 FOR $4.90", 2.05, 3, 4.90),
        ("MIN 3 GET 33% OFF", 5.85, 3, 11.76),  # 3 x 5.85 x 0.67 = 11.7585
        ("3 FOR 30% OFF", 6.55, 3, 13.76),  # 13.755 rounds half up
    ],
)
def test_multibuy_labels(label, price, qty, total):
    assert parse_multibuy([label], price) == MultiBuy(label, qty, total)


@pytest.mark.parametrize(
    "label",
    [
        "$50 OFF $340",  # basket promo
        "SAVE 20%",  # already reflected in price
        "3X BONUS POINTS",
        "BEST BUY",
        "$1 DEAL",
        "1 FOR $5",
        "",
        None,
    ],
)
def test_other_labels_are_ignored(label):
    assert parse_multibuy([label], 6.35) is None


def test_deal_that_is_not_cheaper_is_ignored():
    assert parse_multibuy(["2 FOR $14"], 6.35) is None


def test_picks_best_deal_per_unit():
    deal = parse_multibuy(["2 FOR $11.80", "MIN 3 GET 33% OFF"], 6.35)
    assert deal.qty == 3  # 3 x 6.35 x 0.67 = 12.76, i.e. 4.25 each vs 5.90
