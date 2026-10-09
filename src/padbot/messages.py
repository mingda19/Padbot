"""Everything the bot says: text, keyboards, relative ages. No I/O."""

import html
import sqlite3
from datetime import datetime
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import config
from .sizes import BUCKETS, SizeRange

PARSE_MODE = "HTML"

WELCOME = (
    "Hi! I rank Watsons Singapore sanitary pads by price per pad.\n\n"
    "Send /pad, pick a length, and I'll show the cheapest in stock."
)
ASK_SIZE = "What size?"
ASK_CUSTOM = "Send a length in cm, like 28 or 28-32."
NO_DATA = "I don't have any price data yet. The first update hasn't finished, so please try again later."
NUDGE = "Send /pad to find the cheapest pads."
CANCELLED = "Okay, cancelled. Send /pad whenever you like."


def size_keyboard() -> InlineKeyboardMarkup:
    b = {key: InlineKeyboardButton(size.label, callback_data=f"sz:{key}") for key, size in BUCKETS.items()}
    custom = InlineKeyboardButton("Custom", callback_data="sz:custom")
    return InlineKeyboardMarkup([[b["light"], b["medium"]], [b["heavy"], custom]])


def format_age(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    seconds = (now - then).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)}m ago"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def display_name(row: sqlite3.Row) -> str:
    name, brand = row["name"], row["brand"]
    return f"{brand} {name}" if brand and brand.lower() not in name.lower() else name


def _item(n: int, row: sqlite3.Row) -> str:
    price = _money(row["price"])
    if row["original_price"]:
        price += f" <s>{_money(row['original_price'])}</s>"
    return (
        f"{n}. <b>{html.escape(display_name(row))}</b>\n"
        f"    {_money(row['price_per_pad'])}/pad · {price} · {row['pad_count']} pads · {row['length_cm']:g}cm"
    )


def results_text(rows: list[sqlite3.Row], size: SizeRange, offset: int, age: str) -> str:
    head = f"Top {len(rows)} by price per pad" if offset == 0 else f"Next {len(rows)} by price per pad"
    items = [_item(offset + i + 1, r) for i, r in enumerate(rows)]
    return "\n\n".join(
        [f"<b>{head}</b> · {html.escape(size.label)}", *items, f"<i>Watsons online prices · updated {age}</i>"]
    )


def empty_text(size: SizeRange, offset: int) -> str:
    if offset:
        return "That's everything I have for this size."
    return f"I couldn't find any in-stock pads for {html.escape(size.label)}. Try a wider range with /pad."


def safe_url(url: str) -> str:
    """Telegram rejects URL buttons with raw non-ASCII (Watsons paths contain ®)."""
    return quote(url, safe=":/?&=%#+,;@!$'()*~-._")


def results_keyboard(rows: list[sqlite3.Row], size: SizeRange, offset: int, has_more: bool) -> InlineKeyboardMarkup:
    keyboard = []
    for i, row in enumerate(rows):
        n = offset + i + 1
        buttons = []
        if row["image_url"]:
            buttons.append(InlineKeyboardButton(f"📷 {n}", callback_data=f"pic:{row['product_code']}"))
        if row["url"]:
            buttons.append(InlineKeyboardButton(f"🔗 {n}", url=safe_url(row["url"])))
        if buttons:
            keyboard.append(buttons)
    if has_more:
        label = f"Show {config.RESULTS_PER_PAGE} more"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"more:{size.token}:{offset + len(rows)}")])
    return InlineKeyboardMarkup(keyboard)


def strip_more(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """The same keyboard without its "Show more" row, once that page is shown."""
    rows = [
        row for row in markup.inline_keyboard
        if not any((b.callback_data or "").startswith("more:") for b in row)
    ]  # fmt: skip
    return InlineKeyboardMarkup(rows)


def photo_caption(row: sqlite3.Row) -> str:
    return f"<b>{html.escape(display_name(row))}</b>\n{_money(row['price_per_pad'])}/pad · {_money(row['price'])}"
