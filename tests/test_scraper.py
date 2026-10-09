import json
from pathlib import Path

import httpx
import pytest

from padbot import db
from padbot.scraper import run_scrape
from padbot.watsons import BlockedError, SchemaError, ScrapeError, normalize_product

from conftest import FakeWatsons, clock, make_client, raw_product

def catalogue():
    return {
        "A": [
            raw_product("BP_1", "Super Slimguard Day Wing Sanitary Pad 25cm 14s", 6.1, "14s", "MIN 3 GET 33% OFF", strike=6.55),
            raw_product("BP_2", "Cicada B5 Wing 28cm 13s", 6.3, "13pcs", "2 FOR $9.95"),
            raw_product("BP_3", "Hadaomoi Organic Unscented Liner 52s", 8.3, "52pcs"),
            raw_product("BP_4", "Petite Pads With Organic Cotton Cover 14s", 13.3, "14s"),
            raw_product("BP_5", "Overnight Panties Herbal Anti-Bacterial Size M-L 2s", 3.6, "2s"),
        ],
        "B": [
            raw_product("BP_5", "Overnight Panties Herbal Anti-Bacterial Size M-L 2s", 3.6, "2s"),  # also in A
            raw_product("BP_6", "Love Plus Overnight 36cm Sanitary Pad 10s", 5.5, "10s", stock="outOfStock"),
        ],
    }


def test_happy_path(conn):
    server = FakeWatsons(catalogue())
    result = run_scrape(conn, make_client(server), now=clock)

    assert (result.items_total, result.items_parsed, result.non_pads, result.malformed) == (4, 3, 2, 0)
    assert dict(result.failures) == {"no_length": 1}

    # page size 2: A needs 3 pages, B needs 1
    assert [(r["query"], r["currentPage"]) for r in server.requests] == [
        (":mostRelevant:category:A", "0"), (":mostRelevant:category:A", "1"), (":mostRelevant:category:A", "2"),
        (":mostRelevant:category:B", "0"),
    ]  # fmt: skip
    assert all(r["curr"] == "SGD" and r["fields"] == "FULL" for r in server.requests)

    run = db.last_ok_run(conn)
    assert (run["status"], run["items_total"], run["items_parsed"], run["error"]) == ("ok", 4, 3, None)

    products = {r["product_code"]: r for r in conn.execute("SELECT * FROM products")}
    assert set(products) == {"BP_1", "BP_2", "BP_4", "BP_6"}  # liner and panties never stored

    p = products["BP_1"]
    assert (p["pad_count"], p["length_cm"], p["parse_ok"], p["in_stock"]) == (14, 25.0, 1, 1)
    assert p["price_per_pad"] == pytest.approx(6.1 / 14, abs=1e-4)
    assert p["original_price"] == 6.55
    assert p["brand"] == "LAURIER"
    assert p["url"] == "https://www.watsons.com.sg/some-product/p/BP_1"
    assert p["last_seen_at"] == run["started_at"]
    # MIN 3 GET 33% OFF at $6.10: 3 x 6.10 x 0.67 = 12.26 for 42 pads
    assert (p["promo_text"], p["promo_qty"], p["promo_total"]) == ("MIN 3 GET 33% OFF", 3, 12.26)
    assert p["promo_price_per_pad"] == pytest.approx(12.26 / 42, abs=1e-4)

    assert products["BP_2"]["promo_total"] == 9.95 and products["BP_2"]["original_price"] is None

    # unparseable: stored, flagged, and given no price per pad
    assert (products["BP_4"]["parse_ok"], products["BP_4"]["price_per_pad"], products["BP_4"]["pad_count"]) == (0, None, 14)
    assert products["BP_6"]["in_stock"] == 0


def test_rankings_reflect_the_scrape(conn):
    run_scrape(conn, make_client(FakeWatsons(catalogue())), now=clock)
    ranked = db.ranked_products(conn, 24, 35)
    assert [r["product_code"] for r in ranked] == ["BP_1", "BP_2"]  # BP_6 is out of stock, BP_4 unparsed


def test_403_stops_immediately_and_is_recorded(conn):
    calls = []

    def blocked(request):
        calls.append(request)
        return httpx.Response(403, headers={"server": "AkamaiGHost"}, text="Access Denied")

    with pytest.raises(BlockedError, match="AkamaiGHost"):
        run_scrape(conn, make_client(blocked), now=clock)

    assert len(calls) == 1  # no retry, no second category
    run = conn.execute("SELECT * FROM scrape_runs").fetchone()
    assert run["status"] == "failed" and "BlockedError" in run["error"] and run["finished_at"]
    assert db.last_ok_run(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_429_is_also_not_retried(conn):
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429)

    with pytest.raises(BlockedError):
        run_scrape(conn, make_client(limited), now=clock)
    assert len(calls) == 1


def test_transient_server_errors_are_retried(conn):
    server = FakeWatsons(catalogue())
    attempts = {"n": 0}
    sleeps = []

    def flaky(request):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(503)
        return server(request)

    run_scrape(conn, make_client(flaky, sleep=sleeps.append), now=clock)
    assert db.last_ok_run(conn) is not None
    assert sleeps == [2.0, 4.0]  # backoff between the failed attempts


def test_gives_up_after_repeated_server_errors(conn):
    with pytest.raises(ScrapeError, match="giving up after 3 attempts"):
        run_scrape(conn, make_client(lambda r: httpx.Response(500)), now=clock)
    assert conn.execute("SELECT status FROM scrape_runs").fetchone()[0] == "failed"


def test_requests_are_spaced_by_the_configured_delay(conn):
    sleeps = []
    client = make_client(FakeWatsons(catalogue()), delay=1.0, sleep=sleeps.append)
    run_scrape(conn, client, now=clock)
    assert len(sleeps) == 3  # 4 requests -> 3 gaps
    assert all(0.5 < s <= 1.0 for s in sleeps)


@pytest.mark.parametrize(
    "body",
    [
        {"products": []},  # no pagination
        {"pagination": {"totalPages": 1, "totalResults": 0}},  # no products
        {"pagination": {"totalPages": "1", "totalResults": 0}, "products": []},  # wrong types
        ["not", "an", "object"],
    ],
)
def test_unexpected_response_shape_fails_loudly(conn, body):
    with pytest.raises(SchemaError, match="unexpected response shape"):
        run_scrape(conn, make_client(lambda r: httpx.Response(200, json=body)), now=clock)
    assert conn.execute("SELECT status FROM scrape_runs").fetchone()[0] == "failed"


def test_non_json_response_fails_loudly(conn):
    with pytest.raises(SchemaError, match="not JSON"):
        run_scrape(conn, make_client(lambda r: httpx.Response(200, text="<html>hi</html>")), now=clock)


def test_paging_that_loses_items_fails_loudly(conn):
    def short_changed(request):
        return httpx.Response(
            200,
            json={
                "pagination": {"totalPages": 1, "totalResults": 5},
                "products": [raw_product("BP_1", "Pad 25cm 14s")],
            },
        )

    with pytest.raises(SchemaError, match="reports 5 results, got 1 unique"):
        run_scrape(conn, make_client(short_changed), now=clock)


def test_empty_catalogue_is_a_failure_not_an_empty_success(conn):
    with pytest.raises(SchemaError, match="no pad products"):
        run_scrape(conn, make_client(FakeWatsons({"A": [], "B": []})), now=clock)


def test_a_few_malformed_products_are_skipped_but_many_fail_the_run(conn):
    good = [raw_product(f"BP_{i}", f"Pad {20 + i}cm 14s", 5.0, "14s") for i in range(30)]
    broken = {"code": "BP_X", "name": "No price pad 25cm 14s"}

    result = run_scrape(conn, make_client(FakeWatsons({"A": good + [broken], "B": []})), now=clock)
    assert (result.items_total, result.malformed) == (30, 1)

    with pytest.raises(SchemaError, match="malformed"):
        run_scrape(conn, make_client(FakeWatsons({"A": [dict(broken, code=f"X{i}") for i in range(5)] + good[:5], "B": []})), now=clock)


def test_parse_rate_collapse_keeps_last_good_data(conn):
    run_scrape(conn, make_client(FakeWatsons(catalogue())), now=clock)  # 3/4 parsed
    before = [tuple(r) for r in conn.execute("SELECT * FROM products ORDER BY product_code")]

    # Site changes: names no longer carry length/count.
    changed = {
        "A": [raw_product(c, f"Wingy Pad {c}", 5.0, None) for c in ("BP_1", "BP_2", "BP_4")],
        "B": [raw_product("BP_6", "Wingy Pad BP_6", 5.5, None)],
    }
    with pytest.raises(SchemaError, match="parse rate fell from 75% to 0%"):
        run_scrape(conn, make_client(FakeWatsons(changed)), now=clock)

    assert [tuple(r) for r in conn.execute("SELECT * FROM products ORDER BY product_code")] == before
    statuses = [r[0] for r in conn.execute("SELECT status FROM scrape_runs ORDER BY id")]
    assert statuses == ["ok", "failed"]


def test_delisting_after_two_missed_runs(conn):
    run_scrape(conn, make_client(FakeWatsons(catalogue())), now=clock)
    smaller = catalogue()
    smaller["A"] = [p for p in smaller["A"] if p["code"] != "BP_2"]
    run_scrape(conn, make_client(FakeWatsons(smaller)), now=clock)
    listed = lambda: {r["product_code"] for r in db.ranked_products(conn, 0, 100, limit=50)}  # noqa: E731
    assert "BP_2" in listed()  # missed one run: still listed
    run_scrape(conn, make_client(FakeWatsons(smaller)), now=clock)
    assert "BP_2" not in listed()  # missed two: delisted
    assert "BP_1" in listed()


def test_changed_image_clears_cached_telegram_file_id(conn):
    run_scrape(conn, make_client(FakeWatsons(catalogue())), now=clock)
    conn.execute("UPDATE products SET tg_file_id='FILE'")
    conn.commit()

    same = catalogue()
    run_scrape(conn, make_client(FakeWatsons(same)), now=clock)
    assert conn.execute("SELECT COUNT(*) FROM products WHERE tg_file_id='FILE'").fetchone()[0] == 4

    changed = catalogue()
    changed["A"][0] = raw_product("BP_1", "Super Slimguard Day Wing Sanitary Pad 25cm 14s", 6.1, "14s", image="https://x/new.png?version=2")
    run_scrape(conn, make_client(FakeWatsons(changed)), now=clock)
    got = {r["product_code"]: r["tg_file_id"] for r in conn.execute("SELECT product_code, tg_file_id FROM products")}
    assert got["BP_1"] is None and got["BP_2"] == "FILE"


def test_normalize_real_product_record():
    raw = json.loads((Path(__file__).parent / "fixtures" / "product_bp_72176.json").read_text())
    p = normalize_product(raw)
    assert (p.code, p.brand, p.price, p.original_price, p.in_stock) == ("BP_72176", "PURE N SOFT", 2.05, None, True)
    assert p.url == "https://www.watsons.com.sg/pure-n-soft-dream-v-gentle-overnight-panties-m-l-hip-32-41-waist-up-to-35-2s/p/BP_72176"
    assert p.image_url.endswith("?version=1791130301")
    assert p.content_size_unit == "2s"
    assert p.promo_labels == ("3 FOR $4.90",)  # same label in three fields, kept once


@pytest.mark.parametrize("patch", [{"code": None}, {"name": ""}, {"price": {}}, {"price": {"value": 0}}])
def test_normalize_rejects_malformed(patch):
    with pytest.raises(SchemaError):
        normalize_product({**raw_product("BP_1", "Pad 25cm 14s"), **patch})
