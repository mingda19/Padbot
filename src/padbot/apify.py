"""Product source backed by the crawlerbros/watsons-scraper Apify Actor.

Flow per category (one paid Actor run each):

    POST /actors/{actor}/runs          start, with hard spend caps (never retried)
    GET  /actor-runs/{id}?waitForFinish  poll until a terminal status
    GET  /datasets/{id}/items          read the JSON
    DELETE /datasets/{id}              nothing stays stored on Apify

The raw items are written to config.RAW_DIR first, so if our side fails after a
paid run it can be replayed for free with FileSource.
"""

import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from . import config
from .watsons import RawProduct, SchemaError, ScrapeError

log = logging.getLogger("padbot.apify")

TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyError(ScrapeError):
    pass


@dataclass(frozen=True)
class ApifySettings:
    token: str = field(repr=False)  # never print this
    actor: str = config.APIFY_ACTOR

    @classmethod
    def from_env(cls) -> "ApifySettings":
        """APIFY_TOKEN, or, for convenience, the "Run Actor" URL copied from the
        Apify console (token embedded), kept in APTIFY_RUN_ACTOR_API."""
        token = os.environ.get("APIFY_TOKEN")
        actor = config.APIFY_ACTOR
        if url := os.environ.get("APTIFY_RUN_ACTOR_API"):
            parsed = urlparse(url)
            token = token or (parse_qs(parsed.query).get("token") or [None])[0]
            if m := re.search(r"/(?:actors|acts)/([^/]+)/runs", parsed.path):
                actor = m[1]
        if not token:
            raise ApifyError("no Apify token: set APIFY_TOKEN in .env")
        return cls(token, actor)


# --- item normalisation ------------------------------------------------------
# Field names are from the Actor's README ("Output per product"). Empty fields
# are omitted by the Actor, not null.


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_item(raw: dict) -> RawProduct:
    code, name = raw.get("productCode"), raw.get("name")
    if not isinstance(code, str) or not code or not isinstance(name, str) or not name:
        raise SchemaError(f"item without productCode/name: {str(raw)[:120]}")
    price = _number(raw.get("price"))
    if price is None or price <= 0:
        raise SchemaError(f"{code}: missing or non-positive price")
    was = _number(raw.get("originalPrice"))

    url = raw.get("url")
    if isinstance(url, str) and url.startswith("/"):
        url = config.SITE_BASE + url
    tags = raw.get("promotionTags")
    return RawProduct(
        code=code,
        name=name,
        brand=raw["brand"] if isinstance(raw.get("brand"), str) else None,
        url=url if isinstance(url, str) else None,
        price=price,
        original_price=was if was is not None and was > price else None,
        in_stock=raw.get("stockStatus") in ("inStock", "lowStock"),
        image_url=raw["imageUrl"] if isinstance(raw.get("imageUrl"), str) else None,
        content_size_unit=raw["size"] if isinstance(raw.get("size"), str) else None,
        promo_labels=tuple(dict.fromkeys(t for t in tags if isinstance(t, str)))
        if isinstance(tags, list)
        else (),
    )


def _dedupe(items: Iterable[dict]) -> list[dict]:
    seen: dict[object, dict] = {}
    for i, item in enumerate(items):
        code = item.get("productCode")
        seen.setdefault(code if isinstance(code, str) else ("no-code", i), item)
    return list(seen.values())


# --- replay ------------------------------------------------------------------


class FileSource:
    """Saved Actor items (JSON arrays) instead of a paid run."""

    def __init__(self, paths: Iterable[str | Path]):
        self.paths = [Path(p) for p in paths]

    def fetch_pad_products(self) -> list[dict]:
        items: list[dict] = []
        for path in self.paths:
            data = json.loads(path.read_text())
            if not isinstance(data, list):
                raise SchemaError(f"{path}: expected a JSON array of items")
            items += [i for i in data if isinstance(i, dict)]
        return _dedupe(items)

    def normalize_product(self, raw: dict) -> RawProduct:
        return normalize_item(raw)


# --- live client -------------------------------------------------------------


def _api_message(resp: httpx.Response) -> str:
    try:
        err = resp.json()["error"]
        return f"{err.get('type')}: {err.get('message')}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return resp.text[:200]


class ApifyClient:
    def __init__(
        self,
        settings: ApifySettings,
        *,
        http: httpx.Client | None = None,
        categories: tuple[str, ...] = config.PAD_CATEGORIES,
        max_items: int = config.APIFY_MAX_ITEMS,
        run_timeout_s: int = 300,
        raw_dir: str | Path | None = None,
        keep_raw: int = 20,
        delete_datasets: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=90.0)
        self.categories = categories
        self.max_items = max_items
        self.run_timeout_s = run_timeout_s
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.keep_raw = keep_raw
        self.delete_datasets = delete_datasets
        self._sleep = sleep
        self._monotonic = monotonic
        # In a header, not the URL: httpx logs URLs.
        self._auth = {"Authorization": f"Bearer {settings.token}"}

    def __enter__(self) -> "ApifyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_http:
            self.http.close()

    def normalize_product(self, raw: dict) -> RawProduct:
        return normalize_item(raw)

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, retry: bool, **kwargs) -> httpx.Response:
        """`retry=False` for the paid POST: a retry after a lost response could
        start (and bill) a second run."""
        attempts = 3 if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self.http.request(method, config.APIFY_API + path, headers=self._auth, **kwargs)
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
            else:
                code = resp.status_code
                if code in (401, 403):
                    raise ApifyError(f"Apify rejected the token (HTTP {code}): {_api_message(resp)}")
                if code == 402:
                    raise ApifyError(f"Apify: payment required or usage limit reached: {_api_message(resp)}")
                if code == 429 or code >= 500:
                    problem = f"HTTP {code}"
                elif code >= 400:
                    raise ApifyError(f"{method} {path}: HTTP {code}: {_api_message(resp)}")
                else:
                    return resp
            if attempt == attempts:
                hint = "" if retry else " (the run may or may not have started: check the Apify console)"
                raise ApifyError(f"{method} {path} failed: {problem}{hint}")
            log.warning("%s %s failed (%s), attempt %d/%d", method, path, problem, attempt, attempts)
            self._sleep(2.0 * attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _data(resp: httpx.Response) -> dict:
        try:
            data = resp.json()["data"]
            if isinstance(data, dict):
                return data
        except (ValueError, KeyError, TypeError):
            pass
        raise SchemaError(f"unexpected Apify response: {resp.text[:200]}")

    # -- one run -------------------------------------------------------------

    def _start(self, category: str) -> dict:
        cap = self.max_items
        # Worst case is start fee + a full dataset; round the cap *up* to the cent
        # so it can never cut a legitimate run short.
        worst_case = config.APIFY_START_FEE_USD + cap * config.APIFY_PRICE_PER_RESULT_USD
        spend_cap = math.ceil(worst_case * 100 - 1e-9) / 100
        resp = self._request(
            "POST",
            f"/actors/{self.settings.actor}/runs",
            retry=False,
            params={"maxItems": cap, "maxTotalChargeUsd": spend_cap, "timeout": self.run_timeout_s},
            json={
                "mode": "bycategory",
                "market": "sg",
                "categorySlug": category,
                "sortBy": "mostRelevant",
                "maxItems": cap,
            },
        )
        run = self._data(resp)
        if not run.get("id") or not run.get("defaultDatasetId"):
            raise SchemaError(f"run response lacks id/defaultDatasetId: {str(run)[:200]}")
        log.info("apify run %s started for category %s (spend cap $%.2f)", run["id"], category, spend_cap)
        return run

    def _wait(self, run_id: str) -> dict:
        deadline = self._monotonic() + self.run_timeout_s + 60
        while True:
            run = self._data(
                self._request("GET", f"/actor-runs/{run_id}", retry=True, params={"waitForFinish": 30})
            )
            if run.get("status") in TERMINAL:
                return run
            if self._monotonic() > deadline:
                self._request("POST", f"/actor-runs/{run_id}/abort", retry=True)  # stop the billing
                raise ApifyError(f"run {run_id} still {run.get('status')} after {self.run_timeout_s}s; aborted it")
            self._sleep(1.0)

    def _items(self, dataset_id: str) -> list[dict]:
        resp = self._request(
            "GET",
            f"/datasets/{dataset_id}/items",
            retry=True,
            params={"format": "json", "clean": "true", "limit": self.max_items + 1},
        )
        try:
            items = resp.json()
        except ValueError as exc:
            raise SchemaError(f"dataset items are not JSON: {exc}") from exc
        if not isinstance(items, list):
            raise SchemaError("dataset items are not a JSON array")
        return [i for i in items if isinstance(i, dict)]

    def _forget(self, dataset_id: str) -> None:
        if not self.delete_datasets:
            return
        try:
            self._request("DELETE", f"/datasets/{dataset_id}", retry=True)
        except ScrapeError as exc:
            log.warning("could not delete dataset %s (it will expire on its own): %s", dataset_id, exc)

    def _save_raw(self, category: str, items: list[dict]) -> None:
        if not self.raw_dir:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (self.raw_dir / f"{stamp}-{category}.json").write_text(json.dumps(items))
        for old in sorted(self.raw_dir.glob("*.json"))[: -self.keep_raw]:
            old.unlink()

    def fetch_category(self, category: str) -> list[dict]:
        run = self._start(category)
        try:
            run = self._wait(run["id"])
            log.info(
                "apify run %s %s, platform usage $%s, charged %s",
                run["id"], run["status"], run.get("usageTotalUsd", "?"), run.get("chargedEventCounts", "?"),
            )  # fmt: skip
            if run["status"] != "SUCCEEDED":
                raise ApifyError(f"run {run['id']} ended {run['status']}: {run.get('statusMessage')}")
            items = self._items(run["defaultDatasetId"])
            self._save_raw(category, items)
        finally:
            self._forget(run["defaultDatasetId"])
        if len(items) >= self.max_items:
            raise SchemaError(
                f"category {category}: got {len(items)} items, which is the max_items cap; "
                "results are probably truncated (raise APIFY_MAX_ITEMS)"
            )
        log.info("category %s: %d items", category, len(items))
        return items

    def fetch_pad_products(self) -> list[dict]:
        items: list[dict] = []
        for category in self.categories:
            items += self.fetch_category(category)
        return _dedupe(items)  # a few SKUs are listed in both categories
