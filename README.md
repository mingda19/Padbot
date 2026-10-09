# Padbot

Telegram bot that ranks Watsons SG sanitary pads by price per pad. See [pad-bot-plan.md](pad-bot-plan.md).

Built so far: scraper + DB schema, and the Telegram bot with photos and a scheduled refresh (plan build steps 1-6). Deployment (step 7) is not done.

```
uv venv && uv pip install -e '.[dev]'
pytest                                        # no network, no spend
python -m padbot.scraper                      # PAID Apify run -> $PADBOT_DB (default ./padbot.db)
python -m padbot.scraper --from-file raw/*.json   # replay saved Actor output, free
python -m padbot.bot                          # run the Telegram bot (long polling)
```

Settings go in `.env` (git-ignored):

| Variable | |
|---|---|
| `TELEGRAM_BOT_TOKEN` | required for the bot (from @BotFather) |
| `APIFY_TOKEN`, or `APTIFY_RUN_ACTOR_API` | Apify credentials; the latter is the console's "Run Actor" URL with the token embedded |
| `TELEGRAM_ADMIN_CHAT_ID` | optional; gets an alert when a refresh runs or fails. Message the bot `/id` to find yours |
| `PADBOT_AUTO_REFRESH` | `0` turns the scheduled refresh off (default on) |
| `PADBOT_MIN_RUN_INTERVAL_HOURS` | minimum gap between paid runs (default 336, biweekly) |
| `PADBOT_DB`, `PADBOT_RAW_DIR` | DB path (default `padbot.db`) and saved Actor output (default `raw/`). Relative paths are relative to the **project root**, never to the directory you start the process from |

| File | Role |
|---|---|
| `src/padbot/db.py` | schema, upsert, `scrape_runs` bookkeeping, ranking query |
| `src/padbot/apify.py` | **default source**: runs the Apify Actor, reads its JSON, deletes the dataset; `FileSource` for free replays |
| `src/padbot/watsons.py` | direct Watsons client (blocked, see below), `RawProduct`, error types |
| `src/padbot/parser.py` | name -> `pad_count`, `length_cm`, `is_pad` |
| `src/padbot/promo.py` | multi-buy maths (`2 FOR $9.90`, `MIN 3 GET 33% OFF`) |
| `src/padbot/scraper.py` | `run_scrape`: fetch -> parse -> upsert, one transaction per run |
| `src/padbot/bot.py` | Telegram handlers, the `/pad` conversation, photo caching, the scheduled refresh job |
| `src/padbot/refresh.py` | is a paid refresh due? runs it behind every spend guard |
| `src/padbot/sizes.py`, `messages.py`, `repo.py` | size buckets and custom-length parsing; all bot text and keyboards; the bot's DB reads |

## Spike findings (2026-10-09)

- **Endpoint** (SAP Commerce OCC): `GET https://api.watsons.com.sg/api/v2/wtcsg/products/search?fields=FULL&query=:mostRelevant:category:<code>&pageSize=32&currentPage=<n>&sort=mostRelevant&lang=en&curr=SGD`
- **Categories**: `2503001` Regular Napkins (152 SKUs, 5 pages) and `2503002` Overnight Napkins (65, 3 pages); 2 SKUs are in both. One scrape is 8 requests. The sibling categories (liners, tampons, intimate care, sanitary panties) are not scraped.
- **Fields used**: `code`, `name`, `masterBrand.name`, `price.value`, `strikeThroughPrice`, `stock.stockLevelStatus`, `images[0].url`, `url` (relative), `contentSizeUnit`, `promotionFirstTag`. `purchasable` is `false` for every result, so it is ignored.
- **`contentSizeUnit`** is a structured pack size (`"16s"`, `"8s x 2"`) but is noisy: it disagrees with the name on a few SKUs, and some values have no number (`PIECE`, `PACK`, `1 set`). The name wins; the field is a fallback and a cross-check.
- **Length** is only in the name (`25cm`, sometimes `280mm`). It is absent for the organic-cotton Rael pads and the Molicare lady pads.
- **Real catalogue run** (`tests/fixtures/spike_catalogue.json`): 215 SKUs -> 69 non-pads filtered, 146 pads, 134 parsed (92%). The 12 rejected: 6 no length, 4 name/field count conflict, 2 pad + free item.

## Data source: Apify Actor (`crawlerbros/watsons-scraper`)

Both Watsons hosts return **403 from Akamai** to plain HTTP clients, so `watsons.py` can't be used directly (the pipeline tests still run through it). The Actor does the fetching; we keep only its JSON and all logic stays here.

**Pricing** (pay-per-event, free tier, read from the public Actor listing 2026-10-09): $0.005 per run start + $0.005 per result, plus normal platform usage. A full scan is 217 SKUs in 2 runs, about **$1.10**.

| Cadence | Approx. per month |
|---|---|
| every 6h (the plan) | $130 |
| daily | $33 |
| twice a week | $9 |
| weekly | $4.70 |
| **biweekly (default guard)** | **$2.40** |

**Endpoints used**, from the Actor's API list plus the platform's standard run/dataset endpoints:

| Endpoint | Decision |
|---|---|
| `POST /runs` | **use**, never retried; carries `maxItems` and `maxTotalChargeUsd` spend caps |
| `GET /actor-runs/{id}?waitForFinish=30` | **use** to poll, instead of holding one long connection |
| `GET /datasets/{id}/items`, `DELETE /datasets/{id}` | **use**: read the JSON, then delete it so nothing is stored on Apify |
| `run-sync-get-dataset-items` | not used: one call, but the connection can drop mid-run while billing continues, and the paid results are lost |
| `run-sync` (key-value record) | not useful: the Actor writes to the dataset, not `OUTPUT` |
| `runs/last/dataset/items` | free recovery path for a paid run we failed to ingest (`--from-file` on the saved copy is simpler) |
| Get Actor / OpenAPI / versions / builds | read-only metadata, used for this analysis |
| Update/Delete Actor, Update version, Build | owner-only or destructive; never |

**Cost controls enforced in code:**
- `maxItems` (250 per category) and `maxTotalChargeUsd` are set on every run; reaching the item cap is treated as truncation and fails the run.
- Before starting, the client asks **Apify itself** whether a successful run of the Actor happened within `PADBOT_MIN_RUN_INTERVAL_HOURS` (default 336h, i.e. biweekly) and refuses if so. Apify's history is the ground truth for spend, so this holds even if the local DB is empty or wrong. If the history can't be read it refuses too. A refusal is not recorded as a failed run.
- The local DB enforces the same interval against its own last run (any status). `--force` skips both checks. The plan's "refresh in the background when data is stale" must go through this guard.
- A run that ends non-`SUCCEEDED` or hangs past its timeout is aborted so billing stops.
- The token is sent as a header, never in a URL (httpx logs URLs).
- Every run's items are saved to `raw/` (last 20 kept) before the Apify dataset is deleted.

**Verified against real runs (2026-10-09):** `categorySlug` takes the numeric codes (`2503001`/`2503002`, same order as the site); `size` carries the pack count (`"16s"`); `url`, `imageUrl`, `brand`, `originalPrice` (equal to `price` when not discounted), `stockStatus` are as documented. The first full scan returned 151 + 65 items, billed **$1.09** (2 starts + 216 results), and both datasets were gone from Apify afterwards.

**Known gap: multi-buy labels.** The Actor does not return `promotionTags` for the promo labels the site shows (`2 FOR $7`, `MIN 3 GET 33% OFF`); those live in a field it drops. So `promo_*` columns stay NULL and only `original_price` (strikethrough) is available. The multi-buy code is tested and ready if a source ever supplies the labels.

## The bot

`/pad` asks for a size (Light 16-24cm, Medium 24-35cm, Heavy 35cm+, or Custom), then replies with the 5 cheapest in-stock pads per pad, each with **📷 n** (photo) and **🔗 n** (product page) buttons, plus **Show 5 more**. Every reply says how old the prices are and that they are Watsons online prices.

- Custom accepts `28`, `28cm`, `28-32`, `28 - 32`, `28–32`, `28 to 32` (10-60cm, else it asks again). It is inclusive at both ends. A single length means that length ±0.5cm, because pads are listed at 23, 23.5, 24... and an exact match on 28 would hide 28.5. The reply names the range used.
- Photos: the first tap makes Telegram fetch `image_url` and stores the returned `file_id`; later taps reuse it. A `file_id` Telegram rejects is dropped and refetched.
- Old size keyboards keep working after a restart or timeout (the buttons are conversation entry points, not tied to one message).
- The bot only reads the DB. Refreshing is a separate timer, **not** per user request, so spend can't scale with traffic. After each check it queues the next one for the moment a refresh is actually due (about 14 days after a good scrape), and logs the plan at startup, e.g. `database …/padbot.db: 145 products, last good scrape 0.0d ago; next refresh due 2026-10-23 09:08 UTC`. After a failure it looks again in 24h; after 3 failures in a row it stops and alerts the admin (`python -m padbot.scraper --force` once the cause is fixed).
- **The first scrape is never automatic.** With no good run in the database the bot starts nothing and alerts instead; fill it once with `python -m padbot.scraper` (also on a new server). An empty database is much more likely to be the wrong database than a reason to spend.
- Tests run the real Application and ConversationHandler against a fake Telegram transport (`tests/fake_telegram.py`), so they need no network or token.

## Differences from the plan

- **Extra columns** `promo_text`, `promo_qty`, `promo_total`, `promo_price_per_pad`: the plan wants multi-buy maths but had nowhere to store it. `price_per_pad` stays the single-unit price, as in the plan's ranking query.
- **Delisting**: the plan's prose (missed 2+ runs) and its query (`last_seen_at >= latest_ok_run_start`, i.e. missed 1) disagree. `db.ranking_cutoff` follows the prose.
- **Non-pads are not stored.** Liners, tampons, period panties, cloth pads and incontinence pads (the plan's list plus `panty/panties`, `cloth pad`, `reusable`, `incontinence`, found in the spike) are dropped. `items_total` counts pads only, so the parse rate is meaningful.
- **Name/field count conflicts** and ranges (`24-28cm`) are `parse_ok = 0` rather than guessed.
- **Refresh trigger**: the plan has a user request kick off a background refresh when data is stale. Here a timer does it, scheduled for the due date (see The bot), so no user action can cost money. The plan's "scrape on startup if stale" is dropped for the same reason: the first scrape is manual.
- **Staleness**: the plan's 6h refresh would cost ~$130/month, so freshness is now a budget decision (`PADBOT_MIN_RUN_INTERVAL_HOURS`), and "Prices as of ..." matters more.
- **Failure handling**: a run is all-or-nothing (products and the `ok` marker commit together). A run is also discarded, keeping the old data, if the parse rate drops more than 25 points versus the previous ok run, or if the number of pads falls by more than 30%, or if the response shape or item counts are off.
- `tg_file_id` is cleared whenever `image_url` changes, on every scrape, instead of in a separate monthly check.
