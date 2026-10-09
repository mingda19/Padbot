import os

from dotenv import load_dotenv

load_dotenv()  # .env never overrides variables already set in the environment

DB_PATH = os.environ.get("PADBOT_DB", "padbot.db")

API_BASE = "https://api.watsons.com.sg/api/v2/wtcsg"
SITE_BASE = "https://www.watsons.com.sg"

# Found in the spike: Feminine Care (2503000) also holds liners, tampons,
# intimate wash and period panties, so only the two napkin sub-categories are
# scraped. A few non-pads still sit inside them; parser.is_pad filters those.
PAD_CATEGORIES = ("2503001", "2503002")  # Regular Napkins, Overnight Napkins
PAGE_SIZE = 32

# Plan: <=1 request/sec, an ordinary UA, stop on 429/403.
REQUEST_DELAY_S = 1.0
USER_AGENT = os.environ.get(
    "PADBOT_USER_AGENT",
    "Mozilla/5.0 (compatible; padbot/0.1; personal price comparison)",
)

# Plausible pad length. Matches the 10-60cm custom-size validation in the plan
# and also rejects thickness figures such as "0.07cm".
MIN_LENGTH_CM = 10.0
MAX_LENGTH_CM = 60.0

# A run is rejected (old data kept) if the parse rate falls by more than this
# versus the previous ok run: the site format has probably changed.
MAX_PARSE_RATE_DROP = 0.25
# ...or if the number of pads falls by more than this share. The Apify Actor
# gives no "total results" to cross-check paging against.
MAX_ITEM_DROP = 0.30

# --- Apify (crawlerbros/watsons-scraper, pay-per-event) -------------------
# Direct calls to Watsons are 403'd by Akamai, so the Actor fetches and we only
# keep its JSON. Billing is per result emitted, so everything here is about
# bounding spend. Prices are the Actor's FREE-tier rates (checked 2026-10-09).
APIFY_API = "https://api.apify.com/v2"
APIFY_ACTOR = "crawlerbros~watsons-scraper"
APIFY_START_FEE_USD = 0.005
APIFY_PRICE_PER_RESULT_USD = 0.005
# Per category run. The spike saw 152 and 65 SKUs. Also the hard spend ceiling
# (start fee + cap x price), and reaching it is treated as truncation.
APIFY_MAX_ITEMS = 250
# Refuse to start a paid run sooner than this after the previous one. Biweekly
# keeps a ~$1.10 full scan to roughly $2.40/month.
MIN_RUN_INTERVAL_S = int(float(os.environ.get("PADBOT_MIN_RUN_INTERVAL_HOURS", "336")) * 3600)
# Every paid run's raw items are kept here so a failed ingest can be replayed
# for free (python -m padbot.scraper --from-file ...).
RAW_DIR = os.environ.get("PADBOT_RAW_DIR", "raw")
