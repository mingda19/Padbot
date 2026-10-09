import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# products/scrape_runs follow the plan (section 6). The four promo_* columns are
# an addition: the plan wants "2 for $X" maths but had nowhere to store it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  product_code        TEXT PRIMARY KEY,   -- Watsons internal code
  name                TEXT NOT NULL,
  brand               TEXT,
  url                 TEXT,
  price               REAL NOT NULL,      -- current selling price, SGD
  original_price      REAL,               -- pre-discount, nullable
  pad_count           INTEGER,
  length_cm           REAL,
  price_per_pad       REAL,               -- price / pad_count, computed at upsert
  in_stock            INTEGER,            -- online stock flag
  parse_ok            INTEGER NOT NULL DEFAULT 0,
  image_url           TEXT,
  tg_file_id          TEXT,               -- Telegram's cached photo id, set after first send
  image_checked_at    TEXT,
  last_seen_at        TEXT NOT NULL,      -- updated every scrape run that returns this product
  promo_text          TEXT,               -- raw multi-buy label, e.g. "2 FOR $9.90"
  promo_qty           INTEGER,            -- units that must be bought to get the promo
  promo_total         REAL,               -- total price for promo_qty units
  promo_price_per_pad REAL                -- promo_total / (promo_qty * pad_count)
);

CREATE INDEX IF NOT EXISTS idx_rank
  ON products (parse_ok, in_stock, length_cm, price_per_pad);

CREATE TABLE IF NOT EXISTS scrape_runs (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL,             -- running | ok | failed
  items_total  INTEGER,
  items_parsed INTEGER,
  error        TEXT
);
"""


@dataclass
class ProductRow:
    product_code: str
    name: str
    brand: str | None
    url: str | None
    price: float
    original_price: float | None
    pad_count: int | None
    length_cm: float | None
    price_per_pad: float | None
    in_stock: int | None
    parse_ok: int
    image_url: str | None
    last_seen_at: str
    promo_text: str | None = None
    promo_qty: int | None = None
    promo_total: float | None = None
    promo_price_per_pad: float | None = None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the bot read while a scrape is writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def start_run(conn: sqlite3.Connection, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, status) VALUES (?, 'running')",
        (started_at,),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    finished_at: str,
    items_total: int | None = None,
    items_parsed: int | None = None,
    error: str | None = None,
) -> None:
    """Does not commit, so an ok run can commit together with its product writes."""
    conn.execute(
        "UPDATE scrape_runs SET status=?, finished_at=?, items_total=?, "
        "items_parsed=?, error=? WHERE id=?",
        (status, finished_at, items_total, items_parsed, error, run_id),
    )


# tg_file_id is Telegram's cache of the image at image_url, so it is dropped
# whenever the URL changes (the "?version=" query param changes with the image).
# This runs on every scrape, which subsumes the plan's monthly check.
_UPSERT = """
INSERT INTO products (
  product_code, name, brand, url, price, original_price, pad_count, length_cm,
  price_per_pad, in_stock, parse_ok, image_url, image_checked_at, last_seen_at,
  promo_text, promo_qty, promo_total, promo_price_per_pad
) VALUES (
  :product_code, :name, :brand, :url, :price, :original_price, :pad_count,
  :length_cm, :price_per_pad, :in_stock, :parse_ok, :image_url,
  :last_seen_at, :last_seen_at,
  :promo_text, :promo_qty, :promo_total, :promo_price_per_pad
)
ON CONFLICT(product_code) DO UPDATE SET
  name=excluded.name, brand=excluded.brand, url=excluded.url,
  price=excluded.price, original_price=excluded.original_price,
  pad_count=excluded.pad_count, length_cm=excluded.length_cm,
  price_per_pad=excluded.price_per_pad, in_stock=excluded.in_stock,
  parse_ok=excluded.parse_ok,
  tg_file_id=CASE WHEN excluded.image_url IS products.image_url
                  THEN products.tg_file_id ELSE NULL END,
  image_url=excluded.image_url,
  image_checked_at=excluded.last_seen_at,
  last_seen_at=excluded.last_seen_at,
  promo_text=excluded.promo_text, promo_qty=excluded.promo_qty,
  promo_total=excluded.promo_total,
  promo_price_per_pad=excluded.promo_price_per_pad
"""


def upsert_products(conn: sqlite3.Connection, rows: Iterable[ProductRow]) -> None:
    """Does not commit; see finish_run."""
    conn.executemany(_UPSERT, [asdict(r) for r in rows])


def last_run_started_at(conn: sqlite3.Connection) -> str | None:
    """Start of the most recent run of any status (a failed run may still have
    been billed). Used to rate-limit paid runs."""
    row = conn.execute("SELECT started_at FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
    return row["started_at"] if row else None


def last_ok_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The 'Prices as of ...' source: finished_at of the latest ok run."""
    return conn.execute(
        "SELECT * FROM scrape_runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def ranking_cutoff(conn: sqlite3.Connection) -> str | None:
    """Oldest last_seen_at that still counts as listed.

    The plan says a product missing from 2+ consecutive runs is delisted, but
    its ranking query filters on the latest ok run's start, which would delist
    after a single miss. Using the start of the second-latest ok run satisfies
    the prose: seen in either of the last two ok runs means still listed.
    """
    rows = conn.execute(
        "SELECT started_at FROM scrape_runs WHERE status='ok' "
        "ORDER BY id DESC LIMIT 2"
    ).fetchall()
    return rows[-1]["started_at"] if rows else None


def ranked_products(
    conn: sqlite3.Connection,
    min_cm: float,
    max_cm: float,
    *,
    limit: int = 5,
    offset: int = 0,
    max_inclusive: bool = False,
) -> list[sqlite3.Row]:
    """Cheapest-per-pad listing. Buckets are half-open [min, max); the Custom
    option is inclusive on both ends, so it passes max_inclusive=True."""
    cutoff = ranking_cutoff(conn)
    if cutoff is None:
        return []
    upper = "<=" if max_inclusive else "<"
    return conn.execute(
        f"""
        SELECT * FROM products
        WHERE parse_ok = 1 AND in_stock = 1
          AND length_cm >= :min AND length_cm {upper} :max
          AND last_seen_at >= :cutoff
        ORDER BY price_per_pad ASC
        LIMIT :k OFFSET :offset
        """,
        {"min": min_cm, "max": max_cm, "cutoff": cutoff, "k": limit, "offset": offset},
    ).fetchall()
