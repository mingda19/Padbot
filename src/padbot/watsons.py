"""Watsons SG product-search client (SAP Commerce "OCC" JSON, found in the spike).

    GET {API_BASE}/products/search
        ?fields=FULL&query=:mostRelevant:category:<code>
        &pageSize=32&currentPage=<n>&sort=mostRelevant&lang=en&curr=SGD

This is an undocumented endpoint, so it fails loudly: unexpected shapes raise
SchemaError, and 403/429 raise BlockedError and are never retried.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from . import config

log = logging.getLogger("padbot.watsons")


class ScrapeError(Exception):
    pass


class TooSoonError(ScrapeError):
    """A paid run was refused because a recent one exists. Not a failure."""


class BlockedError(ScrapeError):
    """403/429: the site told us to go away. Stop; don't retry or work around it."""


class SchemaError(ScrapeError):
    """The response doesn't look like what we parse: the site format changed."""


class WatsonsClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        categories: tuple[str, ...] = config.PAD_CATEGORIES,
        page_size: int = config.PAGE_SIZE,
        delay: float = config.REQUEST_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ):
        self._owns_http = http is None
        self.http = http or httpx.Client(
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        self.categories = categories
        self.page_size = page_size
        self.delay = delay
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._last_request: float | None = None

    def __enter__(self) -> "WatsonsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    # -- transport ---------------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = time.monotonic()

    def _get_json(self, params: dict[str, Any]) -> Any:
        url = f"{config.API_BASE}/products/search"
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code in (403, 429):
                    raise BlockedError(
                        f"HTTP {resp.status_code} from {resp.headers.get('server', 'server')}; "
                        "stopping rather than retrying"
                    )
                if resp.status_code >= 500:
                    problem = f"HTTP {resp.status_code}"
                elif resp.status_code != 200:
                    raise ScrapeError(f"unexpected HTTP {resp.status_code}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise SchemaError(f"response is not JSON: {exc}") from exc
            log.warning("request failed (%s), attempt %d/%d", problem, attempt, self.max_attempts)
            if attempt == self.max_attempts:
                raise ScrapeError(f"giving up after {attempt} attempts: {problem}")
            self._sleep(2.0 * attempt)
        raise AssertionError("unreachable")

    # -- paging ------------------------------------------------------------

    def fetch_category(self, category: str) -> list[dict]:
        """Every raw product in one category, validated page by page."""
        products: list[dict] = []
        page, total_pages, expected = 0, 1, 0
        while page < total_pages:
            data = self._get_json(
                {
                    "fields": "FULL",
                    "query": f":mostRelevant:category:{category}",
                    "pageSize": self.page_size,
                    "currentPage": page,
                    "sort": "mostRelevant",
                    "lang": "en",
                    "curr": "SGD",
                }
            )
            pagination = data.get("pagination") if isinstance(data, dict) else None
            items = data.get("products") if isinstance(data, dict) else None
            if (
                not isinstance(pagination, dict)
                or not isinstance(items, list)
                or not isinstance(pagination.get("totalPages"), int)
                or not isinstance(pagination.get("totalResults"), int)
            ):
                raise SchemaError(f"category {category} page {page}: unexpected response shape")
            total_pages, expected = pagination["totalPages"], pagination["totalResults"]
            products.extend(p for p in items if isinstance(p, dict))
            page += 1

        # Catches silently changed paging (short pages, duplicated/missed rows).
        unique = len({p.get("code") for p in products})
        if unique != expected:
            raise SchemaError(
                f"category {category}: API reports {expected} results, got {unique} unique products"
            )
        log.info("category %s: %d products in %d pages", category, unique, total_pages)
        return products

    def normalize_product(self, raw: dict) -> "RawProduct":
        return normalize_product(raw)

    def fetch_pad_products(self) -> list[dict]:
        """Raw products from every pad category, de-duplicated by code (a few
        SKUs are listed in both Regular and Overnight)."""
        by_code: dict[Any, dict] = {}
        for category in self.categories:
            for raw in self.fetch_category(category):
                by_code.setdefault(raw.get("code"), raw)
        return list(by_code.values())


# -- normalising one product ------------------------------------------------


@dataclass(frozen=True)
class RawProduct:
    code: str
    name: str
    brand: str | None
    url: str | None
    price: float
    original_price: float | None
    in_stock: bool
    image_url: str | None
    content_size_unit: str | None
    promo_labels: tuple[str, ...]


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _labels(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for k in ("label", "title", "name", "text") if isinstance(v := value.get(k), str)]
    if isinstance(value, list):
        return [label for item in value for label in _labels(item)]
    return []


def normalize_product(raw: dict) -> RawProduct:
    code, name = raw.get("code"), raw.get("name")
    if not isinstance(code, str) or not code or not isinstance(name, str) or not name:
        raise SchemaError(f"product without code/name: {str(raw)[:120]}")
    price = _num(_dig(raw, "price", "value"))
    if price is None or price <= 0:
        raise SchemaError(f"{code}: missing or non-positive price")

    # strikeThroughPrice equals the price when nothing is discounted.
    was = _num(raw.get("strikeThroughPrice"))
    if was is None:
        was = _num(_dig(raw, "elabOldPrice", "value"))

    path = raw.get("url")
    image = next(
        (i["url"] for i in raw.get("images") or [] if isinstance(i, dict) and i.get("url")), None
    )
    brand = _dig(raw, "masterBrand", "name")
    size = raw.get("contentSizeUnit")
    promos = dict.fromkeys(
        _labels(raw.get("promotionFirstTag"))
        + _labels(_dig(raw, "topPromotion", "title"))
        + _labels(_dig(raw, "topPromotion", "tag"))
        + _labels(raw.get("promotionTags"))
    )
    return RawProduct(
        code=code,
        name=name,
        brand=brand if isinstance(brand, str) else None,
        url=config.SITE_BASE + path if isinstance(path, str) and path.startswith("/") else None,
        price=price,
        original_price=was if was is not None and was > price else None,
        # "purchasable" is false for every search result (no store selected),
        # so only the stock level is trusted.
        in_stock=_dig(raw, "stock", "stockLevelStatus") in ("inStock", "lowStock"),
        image_url=image,
        content_size_unit=size if isinstance(size, str) else None,
        promo_labels=tuple(promos),
    )
