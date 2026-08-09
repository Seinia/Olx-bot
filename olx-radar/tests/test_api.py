import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from olx import api
from olx.api import build_search_url, search_all_pages, search_raw
from olx.models import Filters, OwnerType, State

QUERY = "iphone 13"
KYIV_CITY_ID = 268
PRICE_FROM, PRICE_TO = 3000, 8000


def _filters() -> Filters:
    return Filters(
        price_from=PRICE_FROM,
        price_to=PRICE_TO,
        city_id=KYIV_CITY_ID,
        state=State.USED,
        owner_type=OwnerType.PRIVATE,
    )


def test_build_search_url_maps_all_filters():
    url = build_search_url(QUERY, _filters())
    qs = parse_qs(urlparse(url).query)

    assert qs["filter_float_price:from"] == [str(PRICE_FROM)]
    assert qs["filter_float_price:to"] == [str(PRICE_TO)]
    assert qs["city_id"] == [str(KYIV_CITY_ID)]
    assert qs["filter_enum_state[0]"] == ["used"]
    assert qs["owner_type"] == ["private"]
    # S-05: private_business выглядит как правдоподобное имя параметра и отвечает
    # 200, но не фильтрует. Явная проверка на случай, если рефакторинг его вернёт.
    assert "private_business" not in qs


def test_build_search_url_omits_query_param_when_phrase_is_empty():
    # Режим «весь раздел без фразы» (Watch.query == "") -- параметр query не должен
    # уйти вовсе, пустая строка в query расширяла бы выдачу, а не сужала.
    qs = parse_qs(urlparse(build_search_url("", Filters(category_id=108))).query)
    assert "query" not in qs
    assert qs["category_id"] == ["108"]


def test_build_search_url_maps_region_id():
    qs = parse_qs(urlparse(build_search_url(QUERY, Filters(region_id=21))).query)
    assert qs["region_id"] == ["21"]
    # Без сортировки свежее объявление попадает на первую страницу через 5-17 мин
    # (S-08, замер по проду) и AC-4 проваливается. created_at:desc -- условие скорости.
    qs = parse_qs(urlparse(build_search_url(QUERY, Filters())).query)
    assert qs["sort_by"] == ["created_at:desc"]


def _param(offer: dict, key: str) -> dict | None:
    for p in offer["params"]:
        if p["key"] == key:
            return p
    return None


@pytest.mark.network
def test_search_raw_applies_all_filters_ac2():
    time.sleep(2)
    data = search_raw(QUERY, _filters())
    offers = data["data"]
    assert offers

    for offer in offers:
        price = _param(offer, "price")["value"]["value"]
        state = _param(offer, "state")["value"]["key"]

        assert PRICE_FROM <= price <= PRICE_TO
        assert offer["location"]["city"]["id"] == KYIV_CITY_ID
        assert state == "used"
        # Для КАЖДОГО объявления, не для большинства: private_business «фильтрует»
        # 43/52, owner_type -- 52/52 (S-05). Подмена одного на другой обязана уронить тест.
        assert offer["business"] is False


def _fake_full_page(offset: int, limit: int = 40) -> bytes:
    # id по offset, чтобы разные страницы не давали случайных совпадений id.
    body = {"data": [{"id": offset + i} for i in range(limit)]}
    return json.dumps(body).encode()


def test_search_all_pages_stops_at_max_pages(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout: float = 30.0, proxy: str | None = None) -> bytes:
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        return _fake_full_page(int(qs["offset"][0]))

    monkeypatch.setattr(api, "fetch", fake_fetch)
    items = search_all_pages(QUERY, Filters(), max_pages=1)

    # Каждая страница в этой заглушке полная (40 из 40) -- без потолка max_pages
    # код пошёл бы за второй страницей. Единственное, что должно его остановить
    # здесь, -- max_pages=1.
    assert len(calls) == 1
    assert len(items) == 40


def test_search_all_pages_respects_max_pages_when_pages_are_full(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout: float = 30.0, proxy: str | None = None) -> bytes:
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        return _fake_full_page(int(qs["offset"][0]))

    monkeypatch.setattr(api, "fetch", fake_fetch)
    items = search_all_pages(QUERY, Filters(), max_pages=3, limit=40)

    assert len(calls) == 3
    assert len(items) == 120


def test_search_all_pages_stops_on_partial_page(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    pages = [
        {"data": [{"id": i} for i in range(40)]},
        {"data": [{"id": i} for i in range(40, 55)]},  # неполная -- 15 из 40
    ]
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout: float = 30.0, proxy: str | None = None) -> bytes:
        calls.append(url)
        return json.dumps(pages[len(calls) - 1]).encode()

    monkeypatch.setattr(api, "fetch", fake_fetch)
    items = search_all_pages(QUERY, Filters(), max_pages=5, limit=40)

    # Неполная страница -- признак конца выдачи, дальше идти не за чем даже
    # с запасом по max_pages.
    assert len(calls) == 2
    assert len(items) == 55


@pytest.mark.network
def test_search_all_pages_real_query_spans_multiple_pages():
    time.sleep(2)
    items = search_all_pages("iphone", Filters(), max_pages=3)

    assert len(items) > 50
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))
