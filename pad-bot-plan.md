# Pad Price Bot: v1 Plan (Watsons only)

Telegram bot that ranks Watsons Singapore sanitary pads by **price per pad** for a chosen length range.

---

## 1. Scope

**In v1**
- Watsons SG only (watsons.com.sg)
- Disposable sanitary pads only (no liners, tampons, cups, period underwear)
- `/pad` → pick a size range → top K cheapest by price per pad
- Optional product image on request
- Multi-buy promo maths ("2 for $X"), regex needs to be able to handle this

**Out of v1**
- FairPrice, Guardian, Cheers, 7-Eleven
- In-store stock and price (online data only)

---

## 2. User flow

```
User: /pad
Bot:  "What size?"  [Light 16–24cm] [Medium 24–35cm] [Heavy 35cm+] [Custom]
User: taps option (or Custom → types e.g. "28-32")
Bot:  Top 5 by price/pad:
        1. Brand X Ultra Thin 25cm (24s): $0.21/pad, $4.95 total  [📷] [🔗]
        2. ...
      "Prices as of 3h ago"   [Show 5 more]
User: taps 📷 → bot sends the product photo
```

**Size buckets** are half-open so a 24cm pad lands in exactly one bucket:

| Bucket | Range (cm) |
|---|---|
| Light | 16 ≤ L < 24 |
| Medium | 24 ≤ L < 35 |
| Heavy | L ≥ 35 |
| Custom | user's min ≤ L ≤ max |

Custom input validation: accept `28-32`, `28 - 32`, `28`. Reject anything outside 10–60cm and ask again.

---

## 3. Architecture

```
            ┌──────────────────────┐
  schedule  │  Scraper job         │  every 6h (+ on startup if stale)
  ────────► │  fetch → parse → upsert
            └─────────┬────────────┘
                      ▼
               ┌─────────────┐
               │  SQLite DB  │
               └─────┬───────┘
                     ▲  read-only queries (<100ms)
            ┌────────┴─────────────┐
  Telegram ◄┤  Bot (PTB, async)    │
            └──────────────────────┘
```

**Key decision: the scraper runs on a schedule, not on user requests.**
The bot only reads from the DB. If the last successful run is older than 6h when a user asks (e.g. a scheduled run failed), the bot still serves the cached data, shows "prices as of Xh ago", and triggers one background refresh guarded by a lock. Users never wait for a scrape.

### Stack
- Python 3.12
- `python-telegram-bot` v21: `ConversationHandler` for the flow, `InlineKeyboardMarkup` for buttons, `JobQueue` for the 6h schedule
- `httpx` for calling the Watsons endpoint
- SQLite (single file; plenty for one retailer, a few hundred SKUs)
- Hosting: any always-on small box (a cheap VPS, Railway, Fly.io). Long polling is fine for v1, so no webhook or domain is needed.

---

## 4. Data source

Watsons SG's site is backed by SAP Commerce Cloud (Hybris) JSON endpoints, which third-party scrapers call without auth.

**To do first (spike, ~1h):** open watsons.com.sg → search "pads" / open the sanitary pads category → DevTools Network tab → find the product search JSON call. Record:
- endpoint URL and query params (query, category code, page, pageSize, sort)
- response fields: product code, name, price, original price, stock, image URL, URL, and whether length or count exist as structured attributes
- total result count, so you know how many pages one scrape needs

Be polite: ≤1 request/sec, a normal User-Agent, and stop on 429/403. This is an undocumented endpoint, so it can change without notice and the parser must fail loudly, not silently.

---

## 5. Parsing (the real work)

Watsons names are more consistent than FairPrice, but length and count still mostly live in the product name, e.g. `Brand Ultra Slim Wing Day 25cm 16s` or `... 2 x 14s`.

| Field | Approach | Example patterns |
|---|---|---|
| `length_cm` | regex on name; use a structured attribute if the spike finds one | `(\d{2}(?:\.\d)?)\s?cm` |
| `pad_count` | regex, handle multipacks | `(\d+)\s?(s|pcs|pads)\b`, `(\d+)\s?[x×]\s?(\d+)` → multiply |
| `is_pad` | keyword filter | exclude `liner`, `pantyliner`, `tampon`, `cup`, `pants`, `underwear` |

Rules:
- If length **or** count fails to parse → store with `parse_ok = 0`, exclude from rankings, and log it.
- Mixed packs (e.g. "day + night combo") → `parse_ok = 0` in v1.
- Write unit tests from ~30 real names collected during the spike. This is where most bugs will be.
- After each scrape, log `parsed / total`. If the parse rate drops sharply, the site format probably changed.

---

## 6. Schema

```sql
CREATE TABLE products (
  product_code     TEXT PRIMARY KEY,   -- Watsons internal code
  name             TEXT NOT NULL,
  brand            TEXT,
  url              TEXT,
  price            REAL NOT NULL,      -- current selling price, SGD
  original_price   REAL,               -- pre-discount, nullable
  pad_count        INTEGER,
  length_cm        REAL,
  price_per_pad    REAL,               -- price / pad_count, computed at upsert
  in_stock         INTEGER,            -- online stock flag
  parse_ok         INTEGER NOT NULL DEFAULT 0,
  image_url        TEXT,
  tg_file_id       TEXT,               -- Telegram's cached photo id, set after first send
  image_checked_at TEXT,
  last_seen_at     TEXT NOT NULL       -- updated every scrape run that returns this product
);

CREATE INDEX idx_rank ON products (parse_ok, in_stock, length_cm, price_per_pad);

CREATE TABLE scrape_runs (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL,          -- running | ok | failed
  items_total  INTEGER,
  items_parsed INTEGER,
  error        TEXT
);
```

"Last updated" = `finished_at` of the latest `ok` row in `scrape_runs`. That's one global freshness check, not a per-row timestamp.

Products not seen for 2+ consecutive runs are treated as delisted and excluded from rankings.

**Ranking query**
```sql
SELECT * FROM products
WHERE parse_ok = 1 AND in_stock = 1
  AND length_cm >= :min AND length_cm < :max
  AND last_seen_at >= :latest_ok_run_start
ORDER BY price_per_pad ASC
LIMIT :k OFFSET :offset;
```

---

## 7. Images

Don't store image bytes in the DB.
- Store `image_url` from the scrape.
- First time a user taps 📷: `send_photo(photo=image_url)`. Telegram fetches it, then save the returned `file_id` to `tg_file_id`.
- Later taps: `send_photo(photo=tg_file_id)`. That's instant, with no re-download.
- Monthly: if the image URL changed, clear `tg_file_id`. A price scrape returns the image URL anyway, so this needs no separate job.

---

## 8. Timing per user flow (estimates)

| Step | Expected latency |
|---|---|
| `/pad` → size keyboard appears | ~0.3–1s (Telegram round trip) |
| Tap size → ranked list | ~0.5–1.5s (DB query <100ms + Telegram send) |
| Custom size: extra message exchange | +~0.5–1s bot side (plus user typing time) |
| 📷 first time for a product | ~1–3s (Telegram fetches from Watsons CDN) |
| 📷 cached via `file_id` | ~0.3–1s |

**One full cycle, bot-side: ~1–3s** (excluding the user's own tapping and typing).

For comparison, if the scrape ran inline on the request as originally proposed, the user would wait for the whole scrape. With roughly 100–300 pad SKUs, a few pages, and a polite 1 req/sec, that's an estimated **~5–30s+**. A blocked or slow response could make it a hang. This is why the scrape is scheduled.

Background scrape duration: same ~5–30s estimate. Confirm the actual page count during the spike.

---

## 9. Build order

1. **Spike**: find the endpoint, dump raw JSON for all pad results, collect sample names (~1h)
2. **Parser + tests**: length, count, pad filter (~2–4h)
3. **Scraper job**: fetch all pages → parse → upsert → `scrape_runs` row (~2h)
4. **Bot flow**: `/pad`, size keyboard, custom input, ranked list, pagination (~2–3h)
5. **Images**: 📷 button, `file_id` caching (~1h)
6. **Schedule + stale fallback + lock** (~1h)
7. **Deploy**: always-on host, env var for bot token, log parse rate per run (~1–2h)

Rough total: **1–2 days** of focused work.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Watsons changes or blocks the endpoint | low request rate; alert when a run fails or the parse rate drops |
| Length/count not in the name for some SKUs | exclude with `parse_ok = 0`; review the log weekly early on |
| Online price ≠ in-store price | label results "Watsons online price" |
| Promo prices (multi-buy) not reflected | show `original_price` strikethrough if regex unable to parse |
| ToS grey area | personal/low-volume use, no reselling the data |
