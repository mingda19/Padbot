"""Scheduled scrape job: fetch -> parse -> upsert -> scrape_runs row.

Run once from the shell with `python -m padbot.scraper`. The bot's JobQueue
should call run_scrape via asyncio.to_thread with its own connection.

Products come from a "source": the paid Apify Actor (default), saved Actor
output (--from-file, free), or the direct Watsons client (blocked by Akamai for
non-browser clients, kept for reference).
"""

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from . import config, db
from .apify import ApifyClient, ApifySettings, FileSource
from .parser import ParseResult, parse_product
from .promo import parse_multibuy
from .watsons import RawProduct, SchemaError, ScrapeError, TooSoonError

log = logging.getLogger("padbot.scraper")


class ProductSource(Protocol):
    def fetch_pad_products(self) -> list[dict]: ...

    def normalize_product(self, raw: dict) -> RawProduct: ...


# Skipping one odd SKU is fine; skipping many means the format changed.
MAX_MALFORMED_SHARE = 0.05


@dataclass
class ScrapeResult:
    run_id: int
    items_total: int  # pads seen (liners, panties, tampons etc. excluded)
    items_parsed: int  # of those, parse_ok = 1
    non_pads: int
    malformed: int
    failures: Counter = field(default_factory=Counter)  # parse_ok = 0 by reason

    @property
    def parse_rate(self) -> float:
        return self.items_parsed / self.items_total if self.items_total else 0.0


def build_row(raw: RawProduct, parsed: ParseResult, seen_at: str) -> db.ProductRow:
    ok = parsed.ok
    deal = parse_multibuy(raw.promo_labels, raw.price) if ok else None
    return db.ProductRow(
        product_code=raw.code,
        name=raw.name,
        brand=raw.brand,
        url=raw.url,
        price=raw.price,
        original_price=raw.original_price,
        pad_count=parsed.pad_count,
        length_cm=parsed.length_cm,
        price_per_pad=round(raw.price / parsed.pad_count, 4) if ok else None,
        in_stock=int(raw.in_stock),
        parse_ok=int(ok),
        image_url=raw.image_url,
        last_seen_at=seen_at,
        promo_text=deal.text if deal else None,
        promo_qty=deal.qty if deal else None,
        promo_total=deal.total if deal else None,
        promo_price_per_pad=round(deal.total / (deal.qty * parsed.pad_count), 4) if deal else None,
    )


def _scrape(
    conn: sqlite3.Connection, source: ProductSource, run_id: int, started_at: str, now: Callable[[], str]
) -> ScrapeResult:
    rows: list[db.ProductRow] = []
    result = ScrapeResult(run_id, 0, 0, 0, 0)
    raws = source.fetch_pad_products()

    for raw_json in raws:
        try:
            raw = source.normalize_product(raw_json)
        except SchemaError as exc:
            result.malformed += 1
            log.warning("skipping malformed product: %s", exc)
            continue
        parsed = parse_product(raw.name, raw.content_size_unit)
        if not parsed.is_pad:
            result.non_pads += 1
            continue
        rows.append(build_row(raw, parsed, started_at))
        result.items_total += 1
        if parsed.ok:
            result.items_parsed += 1
        else:
            result.failures[parsed.reason] += 1
            log.warning("parse_fail code=%s reason=%s name=%r", raw.code, parsed.reason, raw.name)

    if result.malformed > MAX_MALFORMED_SHARE * len(raws):
        raise SchemaError(f"{result.malformed}/{len(raws)} products are malformed")
    if result.items_total == 0:
        raise SchemaError("no pad products found")

    # A sharp parse-rate drop means the site changed. Keep the last good data
    # rather than overwrite it with parse_ok = 0 rows.
    prev = db.last_ok_run(conn)
    if prev and prev["items_total"]:
        prev_rate = prev["items_parsed"] / prev["items_total"]
        if result.parse_rate < prev_rate - config.MAX_PARSE_RATE_DROP:
            raise SchemaError(
                f"parse rate fell from {prev_rate:.0%} to {result.parse_rate:.0%}; "
                "site format probably changed (run discarded)"
            )
        # The Actor reports no total to check paging against, so a source that
        # silently returns fewer pages shows up here instead.
        if result.items_total < prev["items_total"] * (1 - config.MAX_ITEM_DROP):
            raise SchemaError(
                f"pads fell from {prev['items_total']} to {result.items_total}; "
                "source probably dropped pages (run discarded)"
            )

    # One transaction: products and the 'ok' marker land together or not at all.
    db.upsert_products(conn, rows)
    db.finish_run(
        conn,
        run_id,
        status="ok",
        finished_at=now(),
        items_total=result.items_total,
        items_parsed=result.items_parsed,
    )
    conn.commit()
    return result


def _refuse_if_too_soon(conn: sqlite3.Connection, started_at: str, min_interval_s: float) -> None:
    last = db.last_run_started_at(conn)
    if not min_interval_s or last is None:
        return
    age = (datetime.fromisoformat(started_at) - datetime.fromisoformat(last)).total_seconds()
    if age < min_interval_s:
        raise TooSoonError(
            f"previous run started {age / 3600:.1f}h ago; minimum interval for paid runs is "
            f"{min_interval_s / 3600:.0f}h (--force to override)"
        )


def run_scrape(
    conn: sqlite3.Connection,
    source: ProductSource,
    *,
    now: Callable[[], str] = db.utcnow,
    min_interval_s: float = 0,
) -> ScrapeResult:
    """One full scrape. Always leaves a finished scrape_runs row (ok or failed);
    on failure the exception is re-raised after being recorded.

    `min_interval_s` is the spend guard for paid sources: if the last run of any
    status started more recently, TooSoonError is raised before anything is
    fetched or recorded."""
    started_at = now()
    _refuse_if_too_soon(conn, started_at, min_interval_s)
    # A source may veto before anything is recorded or paid for (ApifyClient asks
    # Apify itself), so a refusal leaves no 'failed' row behind.
    preflight = getattr(source, "preflight", None)
    if preflight is not None:
        preflight()
    run_id = db.start_run(conn, started_at)
    try:
        result = _scrape(conn, source, run_id, started_at, now)
    except Exception as exc:
        conn.rollback()
        db.finish_run(
            conn,
            run_id,
            status="failed",
            finished_at=now(),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        conn.commit()
        raise
    log.info(
        "scrape run %d ok: parsed %d/%d (%.0f%%), %d non-pads skipped, %d malformed, failures=%s",
        run_id,
        result.items_parsed,
        result.items_total,
        result.parse_rate * 100,
        result.non_pads,
        result.malformed,
        dict(result.failures),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m padbot.scraper", description="Scrape Watsons pads into the DB.")
    ap.add_argument("--from-file", nargs="+", metavar="JSON", help="replay saved Actor items (free) instead of a paid run")
    ap.add_argument("--force", action="store_true", help="skip the minimum-interval guard on paid runs")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    conn = db.connect(config.DB_PATH)
    db.init_db(conn)
    try:
        if args.from_file:
            run_scrape(conn, FileSource(args.from_file))
        else:
            interval = 0 if args.force else config.MIN_RUN_INTERVAL_S
            with ApifyClient(ApifySettings.from_env(), raw_dir=config.RAW_DIR, min_interval_s=interval) as client:
                run_scrape(conn, client, min_interval_s=interval)
    except ScrapeError as exc:
        log.error("scrape failed: %s: %s", type(exc).__name__, exc)
        return 1
    except Exception:
        log.exception("scrape crashed")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
