from datetime import datetime, timedelta, timezone

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from padbot import messages
from padbot.sizes import BUCKETS, SizeInputError, parse_length_input, size_from_token

# --- custom length input -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,lo,hi",
    [
        ("28-32", 28, 32),
        ("28 - 32", 28, 32),
        ("28–32", 28, 32),  # en dash
        ("28 to 32cm", 28, 32),
        ("32-28", 28, 32),  # reversed is fine
        ("27.5-28.5", 27.5, 28.5),
        ("  28cm ", 27.5, 28.5),  # a single length means +-0.5cm
        ("28", 27.5, 28.5),
        ("10", 9.5, 10.5),
    ],
)
def test_custom_input_is_inclusive_and_forgiving(text, lo, hi):
    size = parse_length_input(text)
    assert (size.min_cm, size.max_cm, size.max_inclusive) == (lo, hi, True)


@pytest.mark.parametrize("text", ["9", "61", "10-70", "5-30", "abc", "", "28-", "-5", "28-32-36", "2832", None])
def test_custom_input_rejects_out_of_range_and_garbage(text):
    with pytest.raises(SizeInputError, match="10–60"):
        parse_length_input(text)


def test_buckets_are_half_open_and_contiguous():
    spans = [(b.min_cm, b.max_cm, b.max_inclusive) for b in BUCKETS.values()]
    assert spans == [(16, 24, False), (24, 35, False), (35, 1000, False)]


@pytest.mark.parametrize("text", ["28-32", "27.5-28.5", "28", "10-60"])
def test_tokens_roundtrip_and_fit_in_callback_data(text):
    size = parse_length_input(text)
    again = size_from_token(size.token)
    assert (again.min_cm, again.max_cm, again.max_inclusive) == (size.min_cm, size.max_cm, True)
    assert len(f"more:{size.token}:9999".encode()) <= 64  # Telegram's limit


def test_bucket_tokens_and_unknown_tokens():
    assert size_from_token("medium") is BUCKETS["medium"]
    assert size_from_token("bogus") is None and size_from_token("c1-") is None


# --- messages ------------------------------------------------------------------------


def test_format_age():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    ago = lambda **kw: messages.format_age(now - timedelta(**kw), now)  # noqa: E731
    assert messages.format_age(None, now) == "never"
    assert ago(seconds=30) == "just now"
    assert ago(minutes=5) == "5m ago"
    assert ago(hours=3) == "3h ago"
    assert ago(hours=47) == "47h ago"
    assert ago(days=3) == "3d ago"
    assert ago(days=13, hours=6) == "13d ago"


def test_safe_url_encodes_non_ascii_but_not_existing_escapes():
    url = "https://www.watsons.com.sg/uucare®-ergo-comfort-overnight-panty-xl-3s/p/BP_27710"
    assert messages.safe_url(url) == "https://www.watsons.com.sg/uucare%C2%AE-ergo-comfort-overnight-panty-xl-3s/p/BP_27710"
    assert messages.safe_url("https://x.sg/a%20b?q=1&r=2") == "https://x.sg/a%20b?q=1&r=2"


def test_strip_more_removes_only_the_more_row():
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📷 1", callback_data="pic:A"), InlineKeyboardButton("🔗 1", url="https://x.sg")],
            [InlineKeyboardButton("Show 5 more", callback_data="more:medium:5")],
        ]
    )
    stripped = messages.strip_more(kb)
    assert [[b.text for b in row] for row in stripped.inline_keyboard] == [["📷 1", "🔗 1"]]
