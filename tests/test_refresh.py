import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from padbot import db
from padbot.apify import ApifyError, FileSource
from padbot.refresh import RETRY_S, Outcome, describe, refresh_if_due, seconds_until_next_check

T0 = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def at(delta):
    return lambda: (T0 + delta).isoformat(timespec="seconds")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "r.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    return path


def seed_ok_run(db_path, days_ago):
    """A good scrape that finished `days_ago` days before T0."""
    conn = db.connect(db_path)
    finished = (T0 - days_ago * DAY).isoformat(timespec="seconds")
    run = db.start_run(conn, finished)
    db.finish_run(conn, run, status="ok", finished_at=finished, items_total=1, items_parsed=1)  # matches items_file, so the shrink guard stays quiet
    conn.commit()
    conn.close()


def run_rows(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def items_file(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"productCode": "BP_1", "name": "Wing Pad 25cm 14s", "price": 5.0, "size": "14s", "stockStatus": "inStock"}]))
    return path


class Counting:
    """A source factory that records how often a (paid) source was opened."""

    def __init__(self, source=None, error=None):
        self.source, self.error, self.opened = source, error, 0

    def __call__(self):
        self.opened += 1
        if self.error:
            raise self.error
        return self.source


def broken_source():
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def fetch_pad_products(self):
            raise ApifyError("boom")

        def normalize_product(self, raw):
            raise AssertionError

    return Broken()


# --- the incident: an empty database must never trigger a paid scrape ---------------


def test_an_empty_database_never_triggers_a_paid_scrape(db_path):
    factory = Counting(error=AssertionError("a paid source was opened on an empty database"))
    for later in (timedelta(0), 15 * DAY, 400 * DAY):
        out = refresh_if_due(db_path, source_factory=factory, now=at(later))
        assert out.status == "no_data" and db_path in out.detail and "python -m padbot.scraper" in out.detail
    assert factory.opened == 0 and run_rows(db_path) == 0


def test_even_a_database_that_only_has_failures_is_not_a_reason_to_spend(db_path):
    conn = db.connect(db_path)
    run = db.start_run(conn, T0.isoformat())
    db.finish_run(conn, run, status="failed", finished_at=T0.isoformat(), error="x")
    conn.commit()
    conn.close()
    factory = Counting(error=AssertionError("must not be called"))
    assert refresh_if_due(db_path, source_factory=factory, now=at(30 * DAY)).status == "no_data"


# --- normal operation ------------------------------------------------------------------


def test_fresh_data_never_opens_a_paid_source(db_path):
    seed_ok_run(db_path, days_ago=1)
    factory = Counting(error=AssertionError("must not be called"))
    for later in (timedelta(0), 7 * DAY, 12 * DAY):  # 1 + 12 = 13 days old at the last check
        assert refresh_if_due(db_path, source_factory=factory, now=at(later)).status == "fresh"
    assert factory.opened == 0


def test_refreshes_once_the_data_is_older_than_the_interval(db_path, items_file):
    seed_ok_run(db_path, days_ago=15)
    factory = Counting(FileSource([items_file]))
    out = refresh_if_due(db_path, source_factory=factory, now=at(timedelta(0)))
    assert (out.status, out.detail) == ("ran", "parsed 1/1 pads") and factory.opened == 1
    # ...and the new scrape makes it fresh again
    assert refresh_if_due(db_path, source_factory=factory, now=at(timedelta(hours=1))).status == "fresh"
    assert factory.opened == 1


def test_failure_then_cooldown_then_retry(db_path):
    seed_ok_run(db_path, days_ago=20)
    factory = Counting(broken_source())
    assert refresh_if_due(db_path, source_factory=factory, now=at(timedelta(0))).status == "failed"
    # it may have been billed: no retry for 24h
    assert refresh_if_due(db_path, source_factory=factory, now=at(timedelta(hours=6))).status == "cooldown"
    assert refresh_if_due(db_path, source_factory=factory, now=at(timedelta(hours=25))).status == "failed"


def test_halts_after_three_failures_in_a_row(db_path):
    seed_ok_run(db_path, days_ago=30)
    factory = Counting(broken_source())
    for i in range(3):
        assert refresh_if_due(db_path, source_factory=factory, now=at(timedelta(days=i))).status == "failed"
    out = refresh_if_due(db_path, source_factory=factory, now=at(timedelta(days=3)))
    assert out.status == "halted" and "--force" in out.detail
    assert factory.opened == 3  # the halted check did not touch the source


def test_a_success_resets_the_failure_count(db_path, items_file):
    seed_ok_run(db_path, days_ago=30)
    bad = Counting(broken_source())
    for i in range(2):
        refresh_if_due(db_path, source_factory=bad, now=at(timedelta(days=i)))
    good = Counting(FileSource([items_file]))
    assert refresh_if_due(db_path, source_factory=good, now=at(timedelta(days=2))).status == "ran"
    conn = db.connect(db_path)
    assert db.failures_since_last_ok(conn) == 0
    conn.close()


def test_missing_credentials_are_a_reported_failure_not_a_crash(db_path):
    seed_ok_run(db_path, days_ago=20)
    before = run_rows(db_path)
    out = refresh_if_due(db_path, source_factory=Counting(error=ApifyError("no Apify token")), now=at(timedelta(0)))
    assert out.status == "failed" and "no Apify token" in out.detail
    assert run_rows(db_path) == before  # nothing started, nothing billed


# --- when to wake up ----------------------------------------------------------------------


def test_next_check_is_the_due_date_not_a_poll(db_path):
    seed_ok_run(db_path, days_ago=1)
    assert seconds_until_next_check(db_path, now=at(timedelta(0))) == pytest.approx(13 * 86400)
    seed_ok_run(db_path, days_ago=0)  # a scrape just finished: the full interval again
    assert seconds_until_next_check(db_path, now=at(timedelta(0))) == pytest.approx(14 * 86400)


def test_next_check_when_already_due_is_soon_but_not_instant(db_path):
    seed_ok_run(db_path, days_ago=20)
    assert seconds_until_next_check(db_path, now=at(timedelta(0))) == 60


@pytest.mark.parametrize("status", ["failed", "cooldown", "halted", "no_data"])
def test_after_trouble_or_no_data_look_again_tomorrow(db_path, status):
    seed_ok_run(db_path, days_ago=20)
    assert seconds_until_next_check(db_path, Outcome(status), now=at(timedelta(0))) == RETRY_S


def test_next_check_with_no_data_is_a_daily_look(db_path):
    assert seconds_until_next_check(db_path, now=at(timedelta(0))) == RETRY_S


# --- what startup says about the database -------------------------------------------------------


def test_describe_a_populated_database(db_path):
    seed_ok_run(db_path, days_ago=3)
    level, text = describe(db_path, now=at(timedelta(0)))
    assert level == logging.INFO
    assert db_path in text and "last good scrape 3.0d ago" in text and "next refresh due 2026-10-20 08:00 UTC" in text


def test_describe_an_empty_database_is_a_warning_naming_the_file(db_path):
    level, text = describe(db_path, now=at(timedelta(0)))
    assert level == logging.WARNING and db_path in text and "NO price data" in text
