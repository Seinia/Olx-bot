import json
from pathlib import Path

import pytest

from olx.api import search_all_pages
from olx.models import Filters, OwnerType, State
from olx.parse import matches_filters, parse_search_response

SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "sample-response.json"
)


@pytest.fixture
def listings():
    return parse_search_response(json.loads(SAMPLE.read_text(encoding="utf-8")))


def test_price_outside_range_is_rejected(listings):
    priced = [lst for lst in listings if lst.price is not None]
    low = min(lst.price for lst in priced)
    f = Filters(price_from=low + 1)
    assert not matches_filters(min(priced, key=lambda x: x.price), f)


def test_listing_without_price_fails_price_filter(listings):
    victim = next(lst for lst in listings if lst.price is not None)
    stripped = type(victim)(**{**victim.__dict__, "price": None})
    assert not matches_filters(stripped, Filters(price_from=100))
    assert not matches_filters(stripped, Filters(price_to=100_000))


def test_city_and_category_mismatch_rejected(listings):
    lst = listings[0]
    assert not matches_filters(lst, Filters(city_id=lst.city_id + 1))
    assert not matches_filters(lst, Filters(category_id=(lst.category_id or 0) + 1))


def test_owner_type_both_directions(listings):
    private = next(lst for lst in listings if not lst.business)
    business = next(lst for lst in listings if lst.business)
    assert matches_filters(private, Filters(owner_type=OwnerType.PRIVATE))
    assert not matches_filters(private, Filters(owner_type=OwnerType.BUSINESS))
    assert matches_filters(business, Filters(owner_type=OwnerType.BUSINESS))
    assert not matches_filters(business, Filters(owner_type=OwnerType.PRIVATE))


def test_state_mismatch_rejected(listings):
    used = next(lst for lst in listings if lst.state is State.USED)
    assert not matches_filters(used, Filters(state=State.NEW))


def test_empty_filters_accept_everything(listings):
    assert all(matches_filters(lst, Filters()) for lst in listings)


def test_category_filter_matches_subtree_not_only_exact_id(listings):
    # 963 -- «Acura», лист дерева под родителем 108 («Легкові автомобілі»). Родитель
    # сам объявлений не содержит: режим «весь раздел» ищет по всему поддереву.
    victim = listings[0]
    child = type(victim)(**{**victim.__dict__, "category_id": 963})
    assert matches_filters(child, Filters(category_id=108))
    assert not matches_filters(child, Filters(category_id=222))  # соседний раздел


def test_region_mismatch_rejected(listings):
    lst = listings[0]
    assert not matches_filters(lst, Filters(region_id=(lst.region_id or 0) + 1))
    assert matches_filters(lst, Filters(region_id=lst.region_id))


def test_pagination_stops_when_page_is_all_padding(monkeypatch):
    """OLX добивает исчерпанную выдачу посторонними объявлениями — обход обязан встать."""
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    real = raw["data"][:40]
    # Мусор ровно такой, каким его отдаёт OLX: настоящие объявления из других
    # разделов по бросовой цене, приехавшие в ответ на исчерпанный запрос.
    padding = []
    for i, item in enumerate(raw["data"][:40]):
        junk = json.loads(json.dumps(item))
        junk["id"] = 1_900_000_000 + i
        for p in junk["params"]:
            if p["key"] == "price":
                p["value"]["value"] = 180
        padding.append(junk)

    pages = [{"data": real}, {"data": padding}, {"data": padding}]
    calls = {"n": 0}

    def _search(query, filters, *, offset=0, limit=40, proxy=None):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return page

    monkeypatch.setattr("olx.api.search_raw", _search)
    monkeypatch.setattr("olx.api.time", type("T", (), {"sleep": staticmethod(lambda s: None)}))

    got = search_all_pages("квартира", Filters(price_from=1000, price_to=20000), max_pages=10)

    assert calls["n"] == 2, "обход должен встать на первой же странице из одного мусора"
    assert got, "настоящие результаты первой страницы обязаны остаться"
    assert all(item["id"] < 1_900_000_000 for item in got), "мусор не должен попасть в результат"
