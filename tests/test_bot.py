import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from padbot import db
from padbot.apify import FileSource
from padbot.bot import build_application, refresh_job, schedule_refresh

from fake_telegram import Conversation, FakeTelegram

TOKEN = "123456:TEST-TOKEN-not-real"
NOW = datetime.now(timezone.utc)


def product(code, name, ppp, length, *, count=16, price=None, was=None, stock=1, brand="LAURIER", ok=1, url=None, image="https://img.example/p.png?v=1"):
    return db.ProductRow(
        product_code=code, name=name, brand=brand, url=url or f"https://www.watsons.com.sg/x/p/{code}",
        price=price if price is not None else round(ppp * count, 2), original_price=was, pad_count=count,
        length_cm=length, price_per_pad=ppp, in_stock=stock, parse_ok=ok, image_url=image,
        last_seen_at=(NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
    )  # fmt: skip


CATALOGUE = [
    # Medium 24 <= L < 35: seven in stock, so there are two pages
    product("M1", "Super Slimguard Day Wing 25cm 16s", 0.20, 25.0, was=12.15, url="https://www.watsons.com.sg/uucare®-wing/p/M1"),
    product("M2", "Pad & Wings <Ultra> 28cm 14s", 0.22, 28.0, count=14, brand="KOTEX"),
    product("M3", "Cicada 24cm 16s", 0.24, 24.0),
    product("M4", "Body Fit 29cm 14s", 0.26, 29.0, count=14),
    product("M5", "Extra Dry 26cm 18s", 0.27, 26.0, count=18),
    product("M6", "Love Plus 29cm 14s", 0.30, 29.0, count=14),
    product("M7", "Comfort 30cm 12s", 0.32, 30.0, count=12),
    product("MX", "Sold out 25cm 16s", 0.05, 25.0, stock=0),
    product("MU", "Unparsed pad", 0.01, 25.0, ok=0),
    product("L1", "Light 20cm 24s", 0.18, 20.0, count=24),
    product("H1", "Heavy 40cm 8s", 0.55, 40.0, count=8),
]


def seed(path, products=CATALOGUE, age=timedelta(hours=3)):
    conn = db.connect(path)
    db.init_db(conn)
    started = (NOW - age - timedelta(minutes=1)).isoformat(timespec="seconds")
    run = db.start_run(conn, started)
    db.upsert_products(conn, products)
    db.finish_run(conn, run, status="ok", finished_at=(NOW - age).isoformat(timespec="seconds"), items_total=len(products), items_parsed=len(products))
    conn.commit()
    conn.close()


def run_bot(tmp_path, scenario, *, products=CATALOGUE, setup=None, **app_kwargs):
    """Start the real application on a fake Telegram, run `scenario(chat, fake, db_path)`."""
    db_path = str(tmp_path / "bot.db")
    if products is not None:
        seed(db_path, products)
    else:
        conn = db.connect(db_path)
        db.init_db(conn)
        conn.close()
    fake = FakeTelegram()
    app_kwargs.setdefault("auto_refresh", False)

    async def main():
        app = build_application(TOKEN, db_path, request=fake, **app_kwargs)
        await app.initialize()
        try:
            await scenario(Conversation(app), fake, db_path)
        finally:
            await app.shutdown()

    asyncio.run(main())
    return fake


def texts(fake):
    return [p["text"] for p in fake.sent("sendMessage")]


def buttons(markup):
    return [[(b.get("text"), b.get("callback_data") or b.get("url")) for b in row] for row in markup["inline_keyboard"]]


# --- the /pad flow -----------------------------------------------------------------


def test_commands_are_registered_on_startup(tmp_path):
    from padbot.bot import _post_init

    async def scenario(chat, fake, _):
        await _post_init(chat.app)  # run_polling calls this once; initialize() alone does not

    fake = run_bot(tmp_path, scenario)
    (call,) = fake.sent("setMyCommands")
    assert [(c["command"], c["description"]) for c in call["commands"]] == [
        ("pad", "Find the cheapest sanitary pads"),
        ("cancel", "Cancel the current search"),
    ]


def test_pad_asks_for_a_size(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")

    fake = run_bot(tmp_path, scenario)
    (msg,) = fake.sent("sendMessage")
    assert msg["text"] == "What size?"
    assert buttons(msg["reply_markup"]) == [
        [("Light 16–24cm", "sz:light"), ("Medium 24–35cm", "sz:medium")],
        [("Heavy 35cm+", "sz:heavy"), ("Custom", "sz:custom")],
    ]


def test_bucket_shows_cheapest_five_in_stock(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:medium")

    fake = run_bot(tmp_path, scenario)
    result = fake.sent("sendMessage")[-1]
    text = result["text"]
    assert result["parse_mode"] == "HTML"
    assert text.startswith("<b>Top 5 by price per pad</b> · Medium 24–35cm")
    order = [text.index(f"{n}. ") for n in range(1, 6)]
    assert order == sorted(order)
    assert "1. <b>LAURIER Super Slimguard Day Wing 25cm 16s</b>" in text
    assert "$0.20/pad · $3.20 <s>$12.15</s> · 16 pads · 25cm" in text
    assert "KOTEX Pad &amp; Wings &lt;Ultra&gt; 28cm 14s" in text  # escaped, brand prefixed
    assert "Sold out" not in text and "Unparsed" not in text and "Light" not in text.split("·", 1)[1]
    assert "Comfort 30cm" not in text  # 7th: second page
    assert "updated 3h ago" in text and "Watsons online prices" in text
    assert buttons(result["reply_markup"]) == [
        [("📷 1", "pic:M1"), ("🔗 1", "https://www.watsons.com.sg/uucare%C2%AE-wing/p/M1")],
        *[[(f"📷 {n}", f"pic:M{n}"), (f"🔗 {n}", f"https://www.watsons.com.sg/x/p/M{n}")] for n in range(2, 6)],
        [("Show 5 more", "more:medium:5")],
    ]


def test_show_more_adds_the_next_page_and_removes_the_button(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:medium")
        first = fake.sent("sendMessage")[-1]["reply_markup"]
        await chat.tap("more:medium:5", markup=first)

    fake = run_bot(tmp_path, scenario)
    (edit,) = fake.sent("editMessageReplyMarkup")
    assert all(not (b.get("callback_data") or "").startswith("more:") for row in edit["reply_markup"]["inline_keyboard"] for b in row)
    page2 = fake.sent("sendMessage")[-1]
    assert page2["text"].startswith("<b>Next 2 by price per pad</b>")
    assert "6. <b>LAURIER Love Plus 29cm 14s</b>" in page2["text"] and "7. <b>LAURIER Comfort 30cm 12s</b>" in page2["text"]
    assert buttons(page2["reply_markup"]) == [[("📷 6", "pic:M6"), ("🔗 6", "https://www.watsons.com.sg/x/p/M6")], [("📷 7", "pic:M7"), ("🔗 7", "https://www.watsons.com.sg/x/p/M7")]]


def test_light_and_heavy_buckets(tmp_path):
    async def scenario(chat, fake, _):
        await chat.tap("sz:light")
        await chat.tap("sz:heavy")

    fake = run_bot(tmp_path, scenario)  # also proves old keyboards work without a fresh /pad
    light, heavy = texts(fake)
    assert "Top 1 by price per pad</b> · Light 16–24cm" in light and "Light 20cm 24s" in light
    assert "Top 1 by price per pad</b> · Heavy 35cm+" in heavy and "Heavy 40cm 8s" in heavy


# --- custom length -------------------------------------------------------------------


def test_custom_flow_reprompts_on_bad_input_then_ranks(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.say("99")
        await chat.say("abc")
        await chat.say("26 - 29")

    fake = run_bot(tmp_path, scenario)
    t = texts(fake)
    assert t[1] == "Send a length in cm, like 28 or 28-32."
    assert t[2].startswith("99cm is outside 10–60cm") and t[3].startswith("I couldn't read that")
    assert t[4].startswith("<b>Top 4 by price per pad</b> · 26–29cm")  # M5 26, M2 28, M4 29, M6 29
    assert [n for n in ("Extra Dry", "Pad &amp; Wings", "Body Fit", "Love Plus") if n in t[4]] == ["Extra Dry", "Pad &amp; Wings", "Body Fit", "Love Plus"]
    assert "Cicada" not in t[4] and "Comfort 30cm" not in t[4]  # 24 and 30 are outside; 29 is inside (inclusive)


def test_custom_single_length_means_plus_minus_half_cm(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.say("24")

    fake = run_bot(tmp_path, scenario)
    assert "around 24cm (±0.5)" in texts(fake)[-1] and "Cicada 24cm 16s" in texts(fake)[-1]


def test_custom_results_paginate(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.say("24-35")
        first = fake.sent("sendMessage")[-1]["reply_markup"]
        assert buttons(first)[-1] == [("Show 5 more", "more:c24-35:5")]
        await chat.tap("more:c24-35:5", markup=first)

    fake = run_bot(tmp_path, scenario)
    assert "Next 2 by price per pad</b> · 24–35cm" in texts(fake)[-1]


def test_cancel_ends_the_conversation(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.command("/cancel")
        await chat.say("28")  # no longer read as a length

    fake = run_bot(tmp_path, scenario)
    assert texts(fake)[-2:] == ["Okay, cancelled. Send /pad whenever you like.", "Send /pad to find the cheapest pads."]


def test_pad_restarts_a_half_finished_search(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.command("/pad")
        await chat.tap("sz:heavy")

    fake = run_bot(tmp_path, scenario)
    assert "Heavy 35cm+" in texts(fake)[-1]


# --- empty states ----------------------------------------------------------------------


def test_no_price_data_yet(tmp_path):
    async def scenario(chat, fake, _):
        await chat.tap("sz:medium")

    fake = run_bot(tmp_path, scenario, products=None)
    assert "don't have any price data yet" in texts(fake)[-1]


def test_no_matches_for_a_size(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/pad")
        await chat.tap("sz:custom")
        await chat.say("50-55")

    fake = run_bot(tmp_path, scenario)
    assert "couldn't find any in-stock pads for 50–55cm" in texts(fake)[-1]


def test_id_command_reports_the_chat_id(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/id")

    assert texts(run_bot(tmp_path, scenario)) == ["This chat's id is 42."]


def test_start_and_unprompted_text(tmp_path):
    async def scenario(chat, fake, _):
        await chat.command("/start")
        await chat.say("hello?")

    fake = run_bot(tmp_path, scenario)
    assert "Send /pad" in texts(fake)[0] and texts(fake)[1] == "Send /pad to find the cheapest pads."


# --- photos ------------------------------------------------------------------------------


def cached_id(db_path, code):
    conn = db.connect(db_path)
    try:
        return db.get_product(conn, code)["tg_file_id"]
    finally:
        conn.close()


def test_photo_is_fetched_once_then_served_from_file_id(tmp_path):
    async def scenario(chat, fake, db_path):
        await chat.tap("pic:M1")
        assert cached_id(db_path, "M1") == "FILE_1"
        await chat.tap("pic:M1")

    fake = run_bot(tmp_path, scenario)
    first, second = fake.sent("sendPhoto")
    assert first["photo"] == "https://img.example/p.png?v=1" and second["photo"] == "FILE_1"
    assert first["parse_mode"] == "HTML" and "$0.20/pad" in first["caption"]


def test_a_rejected_file_id_falls_back_to_the_url_and_heals(tmp_path):
    async def scenario(chat, fake, db_path):
        conn = db.connect(db_path)
        db.set_tg_file_id(conn, "M1", "STALE")
        conn.close()
        fake.reject_photos.add("STALE")
        await chat.tap("pic:M1")
        assert cached_id(db_path, "M1") == "FILE_1"

    fake = run_bot(tmp_path, scenario)
    assert [p["photo"] for p in fake.sent("sendPhoto")] == ["STALE", "https://img.example/p.png?v=1"]


def test_photo_that_telegram_cannot_fetch(tmp_path):
    async def scenario(chat, fake, db_path):
        fake.reject_urls = True
        await chat.tap("pic:M1")
        assert cached_id(db_path, "M1") is None

    fake = run_bot(tmp_path, scenario)
    assert texts(fake) == ["Sorry, I couldn't load that photo."]


def test_photo_for_unknown_or_imageless_product(tmp_path):
    async def scenario(chat, fake, _):
        await chat.tap("pic:NOPE")
        await chat.tap("pic:M2")

    fake = run_bot(tmp_path, scenario, products=[product("M2", "No image 28cm 14s", 0.2, 28.0, image=None)])
    assert texts(fake) == ["Sorry, I don't have a photo for that one."] * 2
    assert not fake.sent("sendPhoto")


def test_garbage_callback_data_is_ignored(tmp_path):
    async def scenario(chat, fake, _):
        await chat.tap("more:medium:notanumber")
        await chat.tap("more:nonsense:5")

    fake = run_bot(tmp_path, scenario)
    assert not fake.sent("sendMessage")
    assert len(fake.sent("answerCallbackQuery")) == 2  # buttons still stop their spinner


# --- scheduled refresh ----------------------------------------------------------------------


class FakeJobQueue:
    """Records what the refresh job schedules next."""

    def __init__(self):
        self.scheduled, self.removed = [], []

    def get_jobs_by_name(self, name):
        return [SimpleNamespace(schedule_removal=lambda j=j: self.removed.append(j)) for j in self.scheduled if j["name"] == name][:1] if self.scheduled else []

    def run_once(self, callback, when, name=None, job_kwargs=None):
        self.scheduled.append({"callback": callback, "when": when, "name": name, "job_kwargs": job_kwargs})


def refresh_once(tmp_path, source_factory, *, products, age=timedelta(days=20), rounds=1, admin=777):
    """Run the refresh job `rounds` times; returns (fake telegram, job queue, opened-count)."""
    jobs, opened = FakeJobQueue(), []

    def counting_factory():
        opened.append(1)
        return source_factory()

    async def run():
        db_path = str(tmp_path / "bot.db")
        if products:
            seed(db_path, products, age=age)
        else:
            conn = db.connect(db_path)
            db.init_db(conn)
            conn.close()
        fake = FakeTelegram()
        app = build_application(TOKEN, db_path, request=fake, auto_refresh=False, admin_chat_id=admin, source_factory=counting_factory)
        await app.initialize()
        try:
            for _ in range(rounds):
                await refresh_job(SimpleNamespace(application=app, bot=app.bot, job_queue=jobs))
        finally:
            await app.shutdown()
        return fake

    return asyncio.run(run()), jobs, opened


OLD = [product("M1", "Old 25cm 16s", 0.2, 25.0)]  # one pad, like items_source()


def items_source(tmp_path):
    import json

    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"productCode": "N1", "name": "New Wing 25cm 14s", "price": 5.0, "size": "14s", "stockStatus": "inStock"}]))
    return lambda: FileSource([path])


def test_stale_data_refreshes_once_alerts_the_admin_and_waits_a_full_interval(tmp_path):
    fake, jobs, opened = refresh_once(tmp_path, items_source(tmp_path), products=OLD)
    (alert,) = fake.sent("sendMessage")
    assert alert["chat_id"] == 777 and alert["text"] == "✅ Price data refreshed: parsed 1/1 pads"
    (nxt,) = jobs.scheduled
    assert nxt["when"] == pytest.approx(14 * 86400, abs=120)  # the next check is two weeks out, not hours
    assert nxt["name"] == "refresh" and nxt["job_kwargs"] == {"misfire_grace_time": None}
    assert len(opened) == 1


def test_fresh_data_triggers_nothing_and_waits_until_it_is_due(tmp_path):
    def must_not_open():
        raise AssertionError("a paid source was opened for fresh data")

    fake, jobs, opened = refresh_once(tmp_path, must_not_open, products=OLD, age=timedelta(days=2))
    assert not fake.sent("sendMessage") and not opened
    assert jobs.scheduled[0]["when"] == pytest.approx(12 * 86400, abs=120)


def test_an_empty_database_never_starts_a_paid_run_and_says_so_once(tmp_path):
    """Regression: the bot was started from another folder, got a brand-new empty database,
    treated that as 'refresh due' and spent $1.09. An empty database must only ever raise an alert."""

    def must_not_open():
        raise AssertionError("a paid source was opened on an empty database")

    fake, jobs, opened = refresh_once(tmp_path, must_not_open, products=None, rounds=3)
    assert not opened
    (alert,) = fake.sent("sendMessage")  # once, not on every check
    assert alert["text"].startswith("⚠️ No price data: There is no price data in ") and "bot.db" in alert["text"]
    assert [j["when"] for j in jobs.scheduled] == [86400] * 3  # looks again tomorrow, for free


def test_a_failing_refresh_alerts_once_and_retries_tomorrow(tmp_path):
    from padbot.apify import ApifyError

    def no_token():
        raise ApifyError("no Apify token")

    fake, jobs, _ = refresh_once(tmp_path, no_token, products=OLD, rounds=3)
    alerts = fake.sent("sendMessage")
    assert len(alerts) == 1 and alerts[0]["text"].startswith("⚠️ Refresh failed: ApifyError: no Apify token")
    assert [j["when"] for j in jobs.scheduled] == [86400] * 3


def test_refresh_without_an_admin_id_is_silent(tmp_path):
    fake, _, opened = refresh_once(tmp_path, items_source(tmp_path), products=OLD, admin=None)
    assert not fake.sent("sendMessage") and len(opened) == 1


def test_overlapping_refresh_checks_are_skipped_by_the_lock(tmp_path):
    async def run():
        db_path = str(tmp_path / "bot.db")
        seed(db_path, OLD, age=timedelta(days=20))
        opened, source = [], items_source(tmp_path)
        app = build_application(TOKEN, db_path, request=FakeTelegram(), auto_refresh=False, source_factory=lambda: opened.append(1) or source())
        await app.initialize()
        try:
            ctx = SimpleNamespace(application=app, bot=app.bot, job_queue=FakeJobQueue())
            await asyncio.gather(refresh_job(ctx), refresh_job(ctx))
        finally:
            await app.shutdown()
        return opened

    assert asyncio.run(run()) == [1]


def test_schedule_replaces_any_pending_check(tmp_path):
    db_path = str(tmp_path / "bot.db")
    seed(db_path, OLD, age=timedelta(days=1))
    jobs = FakeJobQueue()
    first = schedule_refresh(jobs, db_path)
    second = schedule_refresh(jobs, db_path)
    assert first == pytest.approx(13 * 86400, abs=120) and second == pytest.approx(first, abs=5)
    assert len(jobs.removed) == 1  # the earlier pending check was cancelled before queueing the new one


def test_starting_the_bot_queues_exactly_one_check(tmp_path):
    async def scenario(chat, fake, db_path):
        assert len(chat.app.job_queue.get_jobs_by_name("refresh")) == 1

    run_bot(tmp_path, scenario, auto_refresh=True)
