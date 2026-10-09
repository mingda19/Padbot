from padbot import db


def row(code, *, length=25.0, ppp=0.30, in_stock=1, ok=1, seen="2026-10-09T10:00:00+00:00", image="img1", **kw):
    return db.ProductRow(
        product_code=code,
        name=f"Pad {code}",
        brand="B",
        url=None,
        price=5.0,
        original_price=None,
        pad_count=16,
        length_cm=length,
        price_per_pad=ppp,
        in_stock=in_stock,
        parse_ok=ok,
        image_url=image,
        last_seen_at=seen,
        **kw,
    )


def ok_run(conn, started, finished=None):
    run_id = db.start_run(conn, started)
    db.finish_run(conn, run_id, status="ok", finished_at=finished or started, items_total=1, items_parsed=1)
    conn.commit()
    return run_id


def test_init_is_idempotent_and_creates_plan_schema(conn):
    db.init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"products", "scrape_runs"} <= tables
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(products)")]
    assert cols[:15] == [  # the plan's columns, in the plan's order
        "product_code", "name", "brand", "url", "price", "original_price", "pad_count",
        "length_cm", "price_per_pad", "in_stock", "parse_ok", "image_url", "tg_file_id",
        "image_checked_at", "last_seen_at",
    ]  # fmt: skip
    idx = [r["name"] for r in conn.execute("PRAGMA index_list(products)")]
    assert "idx_rank" in idx
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_upsert_inserts_then_updates(conn):
    db.upsert_products(conn, [row("A")])
    db.upsert_products(conn, [row("A", ppp=0.25, seen="2026-10-09T16:00:00+00:00")])
    conn.commit()
    r = conn.execute("SELECT * FROM products").fetchall()
    assert len(r) == 1
    assert r[0]["price_per_pad"] == 0.25
    assert r[0]["last_seen_at"] == "2026-10-09T16:00:00+00:00"
    assert r[0]["image_checked_at"] == "2026-10-09T16:00:00+00:00"


def test_tg_file_id_survives_unchanged_image_and_is_cleared_when_it_changes(conn):
    db.upsert_products(conn, [row("A", image="img1")])
    conn.execute("UPDATE products SET tg_file_id='FILE123'")
    db.upsert_products(conn, [row("A", image="img1")])
    assert conn.execute("SELECT tg_file_id FROM products").fetchone()[0] == "FILE123"
    db.upsert_products(conn, [row("A", image="img2")])
    assert conn.execute("SELECT tg_file_id FROM products").fetchone()[0] is None


def test_ranking_cutoff_needs_two_misses_to_delist(conn):
    assert db.ranking_cutoff(conn) is None
    ok_run(conn, "2026-10-09T00:00:00+00:00")
    assert db.ranking_cutoff(conn) == "2026-10-09T00:00:00+00:00"
    ok_run(conn, "2026-10-09T06:00:00+00:00")
    ok_run(conn, "2026-10-09T12:00:00+00:00")
    # failed runs don't count as "runs that could have seen the product"
    failed = db.start_run(conn, "2026-10-09T18:00:00+00:00")
    db.finish_run(conn, failed, status="failed", finished_at="2026-10-09T18:00:05+00:00", error="x")
    conn.commit()
    assert db.ranking_cutoff(conn) == "2026-10-09T06:00:00+00:00"
    assert db.last_ok_run(conn)["finished_at"] == "2026-10-09T12:00:00+00:00"


def test_ranked_products_filters_and_orders(conn):
    ok_run(conn, "2026-10-09T00:00:00+00:00")
    ok_run(conn, "2026-10-09T06:00:00+00:00")
    recent, one_miss, two_misses = "2026-10-09T06:00:00+00:00", "2026-10-09T00:00:00+00:00", "2026-10-08T18:00:00+00:00"
    db.upsert_products(
        conn,
        [
            row("cheap", ppp=0.20, seen=recent),
            row("mid", ppp=0.30, seen=recent),
            row("dear", ppp=0.40, seen=recent),
            row("missed-once", ppp=0.10, seen=one_miss),  # still listed
            row("delisted", ppp=0.05, seen=two_misses),
            row("sold-out", ppp=0.06, in_stock=0, seen=recent),
            row("unparsed", ppp=None, ok=0, seen=recent),
            row("too-short", ppp=0.07, length=20.0, seen=recent),
            row("edge-24", ppp=0.50, length=24.0, seen=recent),
            row("edge-35", ppp=0.55, length=35.0, seen=recent),
        ],
    )
    conn.commit()

    codes = lambda rows: [r["product_code"] for r in rows]  # noqa: E731
    # Medium bucket is [24, 35): 24 is in, 35 is not.
    assert codes(db.ranked_products(conn, 24, 35, limit=10)) == [
        "missed-once", "cheap", "mid", "dear", "edge-24",
    ]  # fmt: skip
    assert codes(db.ranked_products(conn, 24, 35, limit=2, offset=2)) == ["mid", "dear"]
    # Custom ranges are inclusive at the top.
    assert codes(db.ranked_products(conn, 35, 35, limit=10, max_inclusive=True)) == ["edge-35"]
    assert codes(db.ranked_products(conn, 35, 60, limit=10)) == ["edge-35"]  # Heavy: L >= 35
    assert codes(db.ranked_products(conn, 16, 24, limit=10)) == ["too-short"]  # Light: L < 24


def test_ranked_products_is_empty_before_first_successful_scrape(conn):
    db.upsert_products(conn, [row("A")])
    conn.commit()
    assert db.ranked_products(conn, 0, 100) == []
