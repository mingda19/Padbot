# Padbot

Telegram bot that ranks Watsons SG sanitary pads by price per pad. See [pad-bot-plan.md](pad-bot-plan.md).

Built so far: scraper + DB schema (plan build steps 1-3). Bot, images and scheduling are not built yet.

```
uv venv && uv pip install -e '.[dev]'
pytest                                        # no network, no spend
python -m padbot.scraper                      # PAID Apify run -> $PADBOT_DB (default ./padbot.db)
python -m padbot.scraper --from-file raw/*.json   # replay saved Actor output, free
```

Secrets go in `.env` (git-ignored): `APIFY_TOKEN=...` (or the console's "Run Actor" URL in `APTIFY_RUN_ACTOR_API`).

| File | Role |
|---|---|
| `src/padbot/db.py` | schema, upsert, `scrape_runs` bookkeeping, ranking query |
| `src/padbot/apify.py` | **default source**: runs the Apify Actor, reads its JSON, deletes the dataset; `FileSource` for free replays |
| `src/padbot/watsons.py` | direct Watsons client (blocked, see below), `RawProduct`, error types |
| `src/padbot/parser.py` | name -> `pad_count`, `length_cm`, `is_pad` |
| `src/padbot/promo.py` | multi-buy maths (`2 FOR $9.90`, `MIN 3 GET 33% OFF`) |
| `src/padbot/scraper.py` | `run_scrape`: fetch -> parse -> upsert, one transaction per run |

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
- A paid run is refused if the previous run (any status) started less than `PADBOT_MIN_RUN_INTERVAL_HOURS` ago (default 336h, i.e. biweekly; `--force` overrides). The plan's "refresh in the background when data is stale" must go through this guard.
- A run that ends non-`SUCCEEDED` or hangs past its timeout is aborted so billing stops.
- The token is sent as a header, never in a URL (httpx logs URLs).
- Every run's items are saved to `raw/` (last 20 kept) before the Apify dataset is deleted.

**Verified against real runs (2026-10-09):** `categorySlug` takes the numeric codes (`2503001`/`2503002`, same order as the site); `size` carries the pack count (`"16s"`); `url`, `imageUrl`, `brand`, `originalPrice` (equal to `price` when not discounted), `stockStatus` are as documented. The first full scan returned 151 + 65 items, billed **$1.09** (2 starts + 216 results), and both datasets were gone from Apify afterwards.

**Known gap: multi-buy labels.** The Actor does not return `promotionTags` for the promo labels the site shows (`2 FOR $7`, `MIN 3 GET 33% OFF`); those live in a field it drops. So `promo_*` columns stay NULL and only `original_price` (strikethrough) is available. The multi-buy code is tested and ready if a source ever supplies the labels.

## Differences from the plan

- **Extra columns** `promo_text`, `promo_qty`, `promo_total`, `promo_price_per_pad`: the plan wants multi-buy maths but had nowhere to store it. `price_per_pad` stays the single-unit price, as in the plan's ranking query.
- **Delisting**: the plan's prose (missed 2+ runs) and its query (`last_seen_at >= latest_ok_run_start`, i.e. missed 1) disagree. `db.ranking_cutoff` follows the prose.
- **Non-pads are not stored.** Liners, tampons, period panties, cloth pads and incontinence pads (the plan's list plus `panty/panties`, `cloth pad`, `reusable`, `incontinence`, found in the spike) are dropped. `items_total` counts pads only, so the parse rate is meaningful.
- **Name/field count conflicts** and ranges (`24-28cm`) are `parse_ok = 0` rather than guessed.
- **Staleness**: the plan's 6h refresh would cost ~$130/month, so freshness is now a budget decision (`PADBOT_MIN_RUN_INTERVAL_HOURS`), and "Prices as of ..." matters more.
- **Failure handling**: a run is all-or-nothing (products and the `ok` marker commit together). A run is also discarded, keeping the old data, if the parse rate drops more than 25 points versus the previous ok run, or if the number of pads falls by more than 30%, or if the response shape or item counts are off.
- `tg_file_id` is cleared whenever `image_url` changes, on every scrape, instead of in a separate monthly check.
