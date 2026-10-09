"""Decides whether a (paid) refresh is due, and runs it with every guard on.

The bot calls this from a timer, never per user request, so spend is bounded by
the schedule, not by traffic:

  * NEVER the first scrape: with no good run in the DB nothing is started, since
    an empty DB is far more likely to be the wrong DB than a reason to spend
    (that mistake cost $1.09 once). Fill it deliberately with
    `python -m padbot.scraper`.
  * due only when the last ok run is older than MIN_RUN_INTERVAL_S (biweekly)
  * Apify's own history is checked too (ApifyClient.preflight), independent of the DB
  * after a failed attempt, wait REFRESH_FAILURE_COOLDOWN_S (it may have been billed)
  * after REFRESH_MAX_FAILURES failures in a row, stop until someone looks
    (fix the cause, then `python -m padbot.scraper --force`)
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config, db
from .apify import ApifyClient, ApifySettings
from .scraper import ProductSource, run_scrape
from .watsons import TooSoonError

log = logging.getLogger("padbot.refresh")

RETRY_S = 24 * 3600  # how soon to look again after a failure, a refusal, or no data
MIN_DELAY_S = 60


@dataclass(frozen=True)
class Outcome:
    status: str  # fresh | ran | failed | cooldown | halted | no_data
    detail: str = ""


def default_source() -> AbstractContextManager[ProductSource]:
    return ApifyClient(ApifySettings.from_env(), raw_dir=config.RAW_DIR)


def _age_s(now: str, then: str) -> float:
    return (datetime.fromisoformat(now) - datetime.fromisoformat(then)).total_seconds()


def refresh_if_due(
    db_path: str,
    *,
    source_factory: Callable[[], AbstractContextManager[ProductSource]] = default_source,
    now: Callable[[], str] = db.utcnow,
    interval_s: float = config.MIN_RUN_INTERVAL_S,
    cooldown_s: float = config.REFRESH_FAILURE_COOLDOWN_S,
    max_failures: int = config.REFRESH_MAX_FAILURES,
) -> Outcome:
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        last_ok = db.last_ok_run(conn)
        if last_ok is None:
            return Outcome(
                "no_data",
                f"There is no price data in {db_path}, and the first scrape is never started automatically "
                "because it costs money. If this is the right database, run `python -m padbot.scraper` once.",
            )
        if _age_s(now(), last_ok["finished_at"]) < interval_s:
            return Outcome("fresh")
        failures = db.failures_since_last_ok(conn)
        if failures >= max_failures:
            return Outcome(
                "halted",
                f"{failures} failed refreshes in a row; not retrying automatically. "
                "Check the logs, then run `python -m padbot.scraper --force`.",
            )
        try:
            with source_factory() as source:
                result = run_scrape(conn, source, now=now, min_interval_s=cooldown_s)
        except TooSoonError as exc:
            return Outcome("cooldown", str(exc))
        except Exception as exc:  # recorded in scrape_runs by run_scrape when a run started
            log.error("refresh failed: %s: %s", type(exc).__name__, exc)
            return Outcome("failed", f"{type(exc).__name__}: {exc}")
        return Outcome("ran", f"parsed {result.items_parsed}/{result.items_total} pads")
    finally:
        conn.close()


def seconds_until_next_check(
    db_path: str,
    last: Outcome | None = None,
    *,
    now: Callable[[], str] = db.utcnow,
    interval_s: float = config.MIN_RUN_INTERVAL_S,
) -> float:
    """When the scheduler should wake next: the moment a refresh becomes due (a
    fresh biweekly scrape means ~14 days), not a fixed poll. After a failure, a
    refusal or no data, look again tomorrow; looking costs nothing."""
    if last is not None and last.status in ("failed", "cooldown", "halted", "no_data"):
        return RETRY_S
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        last_ok = db.last_ok_run(conn)
    finally:
        conn.close()
    if last_ok is None:
        return RETRY_S
    return max(MIN_DELAY_S, interval_s - _age_s(now(), last_ok["finished_at"]))


def describe(db_path: str, *, now: Callable[[], str] = db.utcnow, interval_s: float = config.MIN_RUN_INTERVAL_S) -> tuple[int, str]:
    """(log level, one-line summary) of what this database holds. Logged at
    startup so a wrong or empty database is obvious immediately."""
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        last_ok = db.last_ok_run(conn)
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    finally:
        conn.close()
    if last_ok is None:
        return logging.WARNING, (
            f"database {db_path}: NO price data. If this is the right database, run "
            "`python -m padbot.scraper` once (nothing is scraped automatically)."
        )
    age = _age_s(now(), last_ok["finished_at"])
    due = datetime.fromisoformat(last_ok["finished_at"]) + timedelta(seconds=interval_s)
    return logging.INFO, (
        f"database {db_path}: {products} products, last good scrape {age / 3600 / 24:.1f}d ago; "
        f"next refresh due {due:%Y-%m-%d %H:%M} UTC"
    )
