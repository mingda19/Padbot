from datetime import datetime, timedelta, timezone
from itertools import count

import httpx
import pytest

from padbot import db
from padbot.watsons import WatsonsClient


def raw_product(
    code,
    name,
    price=6.3,
    size=None,
    promo=None,
    stock="inStock",
    strike=None,
    image="https://medias.watsons.com.sg/publishing/WTCSG-1-front-prodcat.png?version=1",
    brand="LAURIER",
):
    """A search-API product in the shape seen in the spike (only fields we use)."""
    p = {
        "code": code,
        "name": name,
        "masterBrand": {"name": brand},
        "price": {"currencyIso": "SGD", "value": price},
        "strikeThroughPrice": strike if strike is not None else price,
        "stock": {"stockLevelStatus": stock},
        "images": [{"url": image}] if image else [],
        "url": f"/some-product/p/{code}",
        "purchasable": False,
    }
    if size is not None:
        p["contentSizeUnit"] = size
    if promo:
        p["promotionFirstTag"] = promo
        p["topPromotion"] = {"title": promo, "tag": {"label": promo}}
    return p


class FakeWatsons:
    """Serves {category: [products]} like the search endpoint, with paging."""

    def __init__(self, catalogue):
        self.catalogue = catalogue
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        self.requests.append(dict(q))
        category = q["query"].rsplit(":", 1)[1]
        size, page = int(q["pageSize"]), int(q["currentPage"])
        items = self.catalogue[category]
        return httpx.Response(
            200,
            json={
                "pagination": {
                    "currentPage": page,
                    "pageSize": size,
                    "totalPages": max(1, -(-len(items) // size)),
                    "totalResults": len(items),
                },
                "products": items[page * size : (page + 1) * size],
            },
        )


def make_client(handler, **kwargs):
    kwargs.setdefault("delay", 0)
    kwargs.setdefault("sleep", lambda s: None)
    kwargs.setdefault("page_size", 2)
    kwargs.setdefault("categories", ("A", "B"))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WatsonsClient(http, **kwargs)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    yield c
    c.close()


_TICKS = count()


def clock():
    """Strictly increasing timestamps: every call is a minute after the last."""
    return (datetime(2026, 10, 9, tzinfo=timezone.utc) + timedelta(minutes=next(_TICKS))).isoformat(
        timespec="seconds"
    )
