import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from padbot import config, db
from padbot.apify import ApifyClient, ApifyError, ApifySettings, FileSource, normalize_item
from padbot.scraper import TooSoonError, run_scrape
from padbot.watsons import SchemaError

from conftest import clock

TOKEN = "apify_api_SECRET123"


def item(code, name, price=6.3, size=None, tags=None, stock="inStock", original=None, image="https://x/i.png?v=1"):
    """An Actor dataset item, shaped like the README's "Output per product"."""
    d = {
        "productCode": code,
        "name": name,
        "url": f"https://www.watsons.com.sg/some-product/p/{code}",
        "price": price,
        "currency": "SGD",
        "brand": "LAURIER",
        "imageUrl": image,
        "stockStatus": stock,
        "market": "sg",
        "scrapedAt": "2026-10-09T08:00:00Z",
    }
    if original:
        d["originalPrice"] = original
    if size:
        d["size"] = size
    if tags:
        d["promotionTags"] = tags
    return d


def catalogue():
    return {
        "A": [
            item("BP_1", "Super Slimguard Day Wing Sanitary Pad 25cm 14s", 6.1, "14s", ["MIN 3 GET 33% OFF"], original=6.55),
            item("BP_2", "Cicada B5 Wing 28cm 13s", 6.3, "13pcs", ["2 FOR $9.95", "BEST BUY"]),
            item("BP_3", "Hadaomoi Organic Unscented Liner 52s", 8.3, "52pcs"),
            item("BP_4", "Petite Pads With Organic Cotton Cover 14s", 13.3, "14s"),
        ],
        "B": [
            item("BP_3", "Hadaomoi Organic Unscented Liner 52s", 8.3, "52pcs"),  # also in A
            item("BP_6", "Love Plus Overnight 36cm Sanitary Pad 10s", 5.5, "10s", stock="outOfStock"),
        ],
    }


class FakeApify:
    """The four Apify endpoints we use, recording every request."""

    def __init__(self, data, statuses=("SUCCEEDED",), post_status=201, past_runs=(), history_status=200):
        self.data, self.statuses, self.post_status = data, list(statuses), post_status
        self.past_runs, self.history_status = list(past_runs), history_status  # startedAt of earlier SUCCEEDED runs
        self.requests, self.runs, self.deleted, self.aborted = [], {}, [], []

    def calls(self, method, fragment=""):
        return [r for r in self.requests if r.method == method and fragment in r.url.path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v2")
        if request.method == "POST" and path.endswith("/runs"):
            if self.post_status != 201:
                return httpx.Response(self.post_status, json={"error": {"type": "x", "message": "boom"}})
            body = json.loads(request.content)
            n = len(self.runs) + 1
            self.runs[f"run{n}"] = (f"ds{n}", body["categorySlug"])
            return httpx.Response(201, json={"data": {"id": f"run{n}", "defaultDatasetId": f"ds{n}", "status": "READY"}})
        if request.method == "GET" and path.endswith("/runs") and path.startswith("/actors/"):
            if self.history_status != 200:
                return httpx.Response(self.history_status, json={"error": {"type": "x", "message": "nope"}})
            assert dict(request.url.params) == {"status": "SUCCEEDED", "desc": "1", "limit": "1"}
            items = [{"id": f"old{i}", "status": "SUCCEEDED", "startedAt": t} for i, t in enumerate(self.past_runs)]
            return httpx.Response(200, json={"data": {"total": len(items), "items": items[:1]}})
        if request.method == "POST" and path.endswith("/abort"):
            self.aborted.append(path)
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and path.startswith("/actor-runs/"):
            run_id = path.rsplit("/", 1)[1]
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(
                200,
                json={"data": {"id": run_id, "status": status, "statusMessage": "msg",
                               "defaultDatasetId": self.runs[run_id][0], "usageTotalUsd": 0.01}},
            )  # fmt: skip
        if request.method == "GET" and "/datasets/" in path:
            dataset_id = path.split("/")[2]
            category = next(c for ds, c in self.runs.values() if ds == dataset_id)
            return httpx.Response(200, json=self.data[category][: int(request.url.params["limit"])])
        if request.method == "DELETE" and path.startswith("/datasets/"):
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def make(fake, tmp_path=None, **kwargs):
    kwargs.setdefault("categories", ("A", "B"))
    kwargs.setdefault("max_items", 10)
    kwargs.setdefault("sleep", lambda s: None)
    if tmp_path is not None:
        kwargs.setdefault("raw_dir", tmp_path / "raw")
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return ApifyClient(ApifySettings(TOKEN, "crawlerbros~watsons-scraper"), http=http, **kwargs)


# --- the run lifecycle ---------------------------------------------------------


def test_end_to_end_into_the_db(conn, tmp_path):
    fake = FakeApify(catalogue())
    result = run_scrape(conn, make(fake, tmp_path), now=clock)

    assert (result.items_total, result.items_parsed, result.non_pads) == (4, 3, 1)  # liner dropped, BP_4 unparsed
    rows = {r["product_code"]: r for r in conn.execute("SELECT * FROM products")}
    assert set(rows) == {"BP_1", "BP_2", "BP_4", "BP_6"}
    assert rows["BP_1"]["pad_count"] == 14 and rows["BP_1"]["original_price"] == 6.55
    assert rows["BP_1"]["promo_text"] == "MIN 3 GET 33% OFF" and rows["BP_1"]["promo_total"] == 12.26
    assert rows["BP_2"]["promo_text"] == "2 FOR $9.95"  # BEST BUY ignored
    assert rows["BP_6"]["in_stock"] == 0 and rows["BP_4"]["parse_ok"] == 0
    assert db.last_ok_run(conn)["items_total"] == 4


def test_run_is_capped_and_token_stays_out_of_urls(tmp_path):
    fake = FakeApify(catalogue())
    make(fake, tmp_path).fetch_category("A")

    (post,) = fake.calls("POST", "/runs")
    assert post.url.path == "/v2/actors/crawlerbros~watsons-scraper/runs"
    # worst case = start fee 0.005 + 10 x 0.005 = $0.055, rounded up to the cent
    assert dict(post.url.params) == {"maxItems": "10", "maxTotalChargeUsd": "0.06", "timeout": "300"}
    assert json.loads(post.content) == {
        "mode": "bycategory", "market": "sg", "categorySlug": "A", "sortBy": "mostRelevant", "maxItems": 10,
    }  # fmt: skip
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)
    assert all(TOKEN not in str(r.url) for r in fake.requests)


def test_polls_until_finished(tmp_path):
    fake = FakeApify(catalogue(), statuses=["RUNNING", "RUNNING", "SUCCEEDED"])
    sleeps = []
    items = make(fake, tmp_path, sleep=sleeps.append).fetch_category("A")
    assert len(items) == 4
    assert len(fake.calls("GET", "/actor-runs/")) == 3
    assert all(r.url.params["waitForFinish"] == "30" for r in fake.calls("GET", "/actor-runs/"))
    assert sleeps == [1.0, 1.0]


def test_nothing_is_left_on_apify_and_raw_items_are_kept_locally(tmp_path):
    fake = FakeApify(catalogue())
    make(fake, tmp_path).fetch_pad_products()

    assert fake.deleted == ["ds1", "ds2"]
    dumps = sorted((tmp_path / "raw").glob("*.json"))
    assert len(dumps) == 2
    assert [i["productCode"] for i in json.loads(dumps[0].read_text())] == ["BP_1", "BP_2", "BP_3", "BP_4"]


def test_raw_dumps_are_pruned(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(5):
        (raw / f"2026010{i}T000000Z-A.json").write_text("[]")
    make(FakeApify(catalogue()), tmp_path, keep_raw=3).fetch_category("A")
    assert len(list(raw.glob("*.json"))) == 3


def test_duplicate_skus_across_categories_are_merged(tmp_path):
    codes = [i["productCode"] for i in make(FakeApify(catalogue()), tmp_path).fetch_pad_products()]
    assert codes == ["BP_1", "BP_2", "BP_3", "BP_4", "BP_6"]


# --- failure handling ----------------------------------------------------------


def test_failed_run_is_an_error_and_still_cleans_up(conn, tmp_path):
    fake = FakeApify(catalogue(), statuses=["FAILED"])
    with pytest.raises(ApifyError, match="ended FAILED: msg"):
        run_scrape(conn, make(fake, tmp_path), now=clock)
    assert fake.deleted == ["ds1"]
    assert conn.execute("SELECT status FROM scrape_runs").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_run_that_never_finishes_is_aborted_to_stop_billing(tmp_path):
    fake = FakeApify(catalogue(), statuses=["RUNNING"])
    ticks = iter(range(0, 10_000, 100))  # monotonic clock jumping 100s per call
    client = make(fake, tmp_path, run_timeout_s=300, monotonic=lambda: next(ticks))
    with pytest.raises(ApifyError, match="aborted"):
        client.fetch_category("A")
    assert fake.aborted == ["/actor-runs/run1/abort"]
    assert fake.deleted == ["ds1"]


def test_paid_post_is_never_retried(tmp_path):
    fake = FakeApify(catalogue(), post_status=500)
    with pytest.raises(ApifyError, match="may or may not have started"):
        make(fake, tmp_path).fetch_category("A")
    assert len(fake.calls("POST")) == 1


def test_reads_are_retried(tmp_path):
    fake = FakeApify(catalogue())
    flaky = {"n": 0}

    def handler(request):
        if request.method == "GET" and "/actor-runs/" in request.url.path:
            flaky["n"] += 1
            if flaky["n"] <= 2:
                return httpx.Response(503)
        return fake(request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ApifyClient(ApifySettings(TOKEN), http=http, categories=("A",), max_items=10, sleep=lambda s: None)
    assert len(client.fetch_category("A")) == 4
    assert flaky["n"] == 3


@pytest.mark.parametrize(
    "status,message",
    [(401, "rejected the token"), (403, "rejected the token"), (402, "payment required")],
)
def test_account_problems_are_reported_plainly(tmp_path, status, message):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"type": "t", "message": "m"}})))
    client = ApifyClient(ApifySettings(TOKEN), http=http, sleep=lambda s: None)
    with pytest.raises(ApifyError, match=message) as exc:
        client.fetch_category("A")
    assert TOKEN not in str(exc.value)


def test_hitting_the_item_cap_is_treated_as_truncation(tmp_path):
    with pytest.raises(SchemaError, match="max_items cap"):
        make(FakeApify(catalogue()), tmp_path, max_items=4).fetch_category("A")  # A has exactly 4


def test_malformed_run_response_fails_loudly(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"data": {"id": "r"}})))
    with pytest.raises(SchemaError, match="defaultDatasetId"):
        ApifyClient(ApifySettings(TOKEN), http=http).fetch_category("A")


# --- settings ------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("APIFY_TOKEN", "APTIFY_RUN_ACTOR_API"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_settings_from_token(clean_env):
    clean_env.setenv("APIFY_TOKEN", TOKEN)
    s = ApifySettings.from_env()
    assert (s.token, s.actor) == (TOKEN, config.APIFY_ACTOR)
    assert TOKEN not in repr(s)


def test_settings_from_the_console_run_url(clean_env):
    clean_env.setenv("APTIFY_RUN_ACTOR_API", f"https://api.apify.com/v2/actors/someone~other-actor/runs?token={TOKEN}")
    s = ApifySettings.from_env()
    assert (s.token, s.actor) == (TOKEN, "someone~other-actor")


def test_settings_without_a_token(clean_env):
    with pytest.raises(ApifyError, match="APIFY_TOKEN"):
        ApifySettings.from_env()


# --- item normalisation ----------------------------------------------------------


def test_normalize_item():
    p = normalize_item(item("BP_9", "Pad 25cm 14s", 6.1, "14s", ["2 FOR $9", "2 FOR $9"], original=6.55))
    assert (p.code, p.price, p.original_price, p.in_stock, p.content_size_unit) == ("BP_9", 6.1, 6.55, True, "14s")
    assert p.promo_labels == ("2 FOR $9",)  # de-duplicated
    assert p.url == "https://www.watsons.com.sg/some-product/p/BP_9"


def test_normalize_item_tolerates_omitted_fields_and_relative_urls():
    p = normalize_item({"productCode": "BP_9", "name": "Pad", "price": 5, "url": "/x/p/BP_9", "stockStatus": "lowStock"})
    assert (p.brand, p.image_url, p.content_size_unit, p.promo_labels, p.original_price) == (None, None, None, (), None)
    assert p.url == "https://www.watsons.com.sg/x/p/BP_9" and p.in_stock is True
    assert normalize_item({"productCode": "BP_9", "name": "Pad", "price": 5}).in_stock is False
    # a "discount" that isn't one is not an original price
    assert normalize_item({"productCode": "B", "name": "P", "price": 5, "originalPrice": 5}).original_price is None


@pytest.mark.parametrize("bad", [{"productCode": None}, {"name": ""}, {"price": None}, {"price": 0}, {"price": "6.30"}])
def test_normalize_item_rejects_malformed(bad):
    with pytest.raises(SchemaError):
        normalize_item({**item("BP_1", "Pad 25cm 14s"), **bad})


# --- replay (free) ---------------------------------------------------------------


def test_replaying_saved_items_costs_nothing(conn, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(catalogue()["A"]))
    (tmp_path / "b.json").write_text(json.dumps(catalogue()["B"]))
    result = run_scrape(conn, FileSource([tmp_path / "a.json", tmp_path / "b.json"]), now=clock)
    assert (result.items_total, result.items_parsed) == (4, 3)


def test_replay_rejects_non_arrays(conn, tmp_path):
    (tmp_path / "x.json").write_text('{"items": []}')
    with pytest.raises(SchemaError, match="JSON array"):
        run_scrape(conn, FileSource([tmp_path / "x.json"]), now=clock)


# --- spend guards ----------------------------------------------------------------


def test_minimum_interval_refuses_a_second_paid_run(conn, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(catalogue()["A"]))
    source = FileSource([tmp_path / "a.json"])
    t0 = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    at = lambda hours: (lambda: (t0 + timedelta(hours=hours)).isoformat(timespec="seconds"))  # noqa: E731

    run_scrape(conn, source, now=at(0), min_interval_s=24 * 3600)  # first run: nothing to compare with
    with pytest.raises(TooSoonError, match="previous run started 6.0h ago"):
        run_scrape(conn, source, now=at(6), min_interval_s=24 * 3600)
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 1  # refusal isn't a run
    run_scrape(conn, source, now=at(25), min_interval_s=24 * 3600)
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 2


def test_failed_runs_count_towards_the_interval(conn):
    class Broken:
        def fetch_pad_products(self):
            raise ApifyError("boom")

    t0 = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(ApifyError):
        run_scrape(conn, Broken(), now=lambda: t0.isoformat(timespec="seconds"))
    with pytest.raises(TooSoonError):  # it may have been billed, so wait it out
        run_scrape(conn, Broken(), now=lambda: (t0 + timedelta(hours=1)).isoformat(timespec="seconds"), min_interval_s=3600 * 24)


def test_a_source_that_silently_loses_half_the_catalogue_is_rejected(conn, tmp_path):
    full = [item(f"BP_{i}", f"Wing Pad {20 + i}cm 14s", 5.0, "14s") for i in range(10)]
    (tmp_path / "full.json").write_text(json.dumps(full))
    (tmp_path / "half.json").write_text(json.dumps(full[:5]))
    run_scrape(conn, FileSource([tmp_path / "full.json"]), now=clock)
    with pytest.raises(SchemaError, match="pads fell from 10 to 5"):
        run_scrape(conn, FileSource([tmp_path / "half.json"]), now=clock)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 10


# --- Apify-side spend guard (independent of the local DB) --------------------------------


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def test_a_recent_paid_run_on_apify_blocks_a_new_one_even_with_an_empty_database(conn, tmp_path):
    """The incident: the bot was pointed at an empty DB, which hid that a full scan had just been paid for."""
    fake = FakeApify(catalogue(), past_runs=[ago(minutes=30)])
    with pytest.raises(TooSoonError, match=r"successful run of this Actor 0\.5h ago"):
        run_scrape(conn, make(fake, tmp_path), now=clock)
    assert not fake.calls("POST")  # nothing was started
    assert conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0] == 0  # a refusal is not a failed run
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_an_old_enough_run_on_apify_does_not_block(conn, tmp_path):
    fake = FakeApify(catalogue(), past_runs=[ago(days=15)])
    run_scrape(conn, make(fake, tmp_path, min_interval_s=14 * 86400), now=clock)
    assert len(fake.calls("POST")) == 2 and db.last_ok_run(conn) is not None


def test_no_history_on_apify_does_not_block(conn, tmp_path):
    run_scrape(conn, make(FakeApify(catalogue()), tmp_path), now=clock)
    assert db.last_ok_run(conn) is not None


def test_force_skips_the_apify_check_entirely(conn, tmp_path):
    fake = FakeApify(catalogue(), past_runs=[ago(minutes=1)])
    run_scrape(conn, make(fake, tmp_path, min_interval_s=0), now=clock)
    assert not [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/runs")]  # not even asked


def test_if_apify_history_cannot_be_read_nothing_is_started(conn, tmp_path):
    fake = FakeApify(catalogue(), history_status=500)
    with pytest.raises(ApifyError):
        run_scrape(conn, make(fake, tmp_path), now=clock)
    assert not fake.calls("POST")  # fails closed


def test_the_history_check_happens_once_not_per_category(conn, tmp_path):
    fake = FakeApify(catalogue())
    run_scrape(conn, make(fake, tmp_path), now=clock)
    history = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/runs")]
    assert len(history) == 1  # a second look would see the first category's run and refuse


def test_unreadable_history_is_a_schema_error(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"items": [{"id": "x"}]}})))
    with pytest.raises(SchemaError, match="startedAt"):
        ApifyClient(ApifySettings(TOKEN), http=http).preflight()
