from __future__ import annotations

from datetime import datetime
from typing import Any

from olx import categories
from olx.errors import SchemaError
from olx.models import Filters, Listing, OwnerType, State

# Карточка в Telegram использует этот размер (контракты, п.1.2).
PHOTO_WIDTH = "800"
PHOTO_HEIGHT = "600"


def _param(params: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    # Порядок params[] не гарантирован (S-01+02) -- индекс использовать нельзя.
    for p in params:
        if p.get("key") == key:
            return p
    return None


def _photo_urls(photos: list[dict[str, Any]]) -> tuple[str, ...]:
    # link -- шаблон вида ".../image;s={width}x{height}", а не готовый URL.
    return tuple(
        photo["link"].replace("{width}", PHOTO_WIDTH).replace("{height}", PHOTO_HEIGHT)
        for photo in photos
    )


def _parse_time(raw: dict[str, Any], field: str) -> datetime | None:
    value = raw.get(field)
    return datetime.fromisoformat(value) if value else None


def parse_listing(raw: dict) -> Listing:
    try:
        params = raw["params"]
        price_param = _param(params, "price")
        state_param = _param(params, "state")

        if price_param is not None:
            price_value = price_param["value"]
            price = price_value["value"]
            currency = price_value["currency"]
            negotiable = price_value["negotiable"]
            arranged = price_value["arranged"]
        else:
            price = currency = None
            negotiable = arranged = False

        # Отсутствует у части категорий (контракты, Listing.state) -- не требуем.
        state = State(state_param["value"]["key"]) if state_param is not None else None

        location = raw["location"]

        photo_urls = _photo_urls(raw.get("photos") or [])

        return Listing(
            id=raw["id"],
            url=raw["url"],
            title=raw["title"],
            description=raw["description"],
            price=price,
            currency=currency,
            negotiable=negotiable,
            arranged=arranged,
            city_id=location["city"]["id"],
            city_name=location["city"]["name"],
            region_id=location["region"]["id"],
            business=raw["business"],
            state=state,
            status=raw["status"],
            created_at=datetime.fromisoformat(raw["created_time"]),
            pushed_at=_parse_time(raw, "pushup_time"),
            refreshed_at=_parse_time(raw, "last_refresh_time"),
            photo_url=photo_urls[0] if photo_urls else None,
            category_id=raw["category"]["id"],
            seller_id=raw["user"]["id"],
            # user.seller_type пустой у всех объявлений на этом эндпоинте (S-01+02) --
            # тип продавца в Listing берётся из business, сюда идёт только имя.
            seller_name=raw["user"]["name"],
            photo_urls=photo_urls,
        )
    except (KeyError, TypeError, ValueError) as e:
        listing_id = raw.get("id", "?") if isinstance(raw, dict) else "?"
        raise SchemaError(f"Не удалось разобрать объявление {listing_id}: {e}") from e


def parse_search_response(raw: dict) -> list[Listing]:
    # Пустой список неотличим от «ничего не нашлось» -- при поломке формы падаем,
    # а не возвращаем [] (контракты, R-8).
    if not isinstance(raw, dict) or "data" not in raw:
        raise SchemaError("В ответе нет ключа 'data' -- структура API изменилась")
    data = raw["data"]
    if not isinstance(data, list):
        raise SchemaError(f"'data' не список, а {type(data).__name__}")
    return [parse_listing(item) for item in data]


def matches_filters(listing: Listing, filters: Filters) -> bool:
    """Проверка выдачи против запрошенных фильтров на нашей стороне.

    Не перестраховка: когда результаты по запросу заканчиваются, OLX не отдаёт пустую
    страницу, а добивает её посторонними «рекомендованными» объявлениями, к фильтрам
    отношения не имеющими -- по «Квартира» за 9000-15000 грн так приезжали входные
    двери за 180 грн. Серверным фильтрам верить нельзя, сверяем сами.
    """
    f = filters
    if f.price_from is not None and (listing.price is None or listing.price < f.price_from):
        return False
    if f.price_to is not None and (listing.price is None or listing.price > f.price_to):
        return False
    if f.city_id is not None and listing.city_id != f.city_id:
        return False
    if f.region_id is not None and listing.region_id != f.region_id:
        return False
    if f.category_id is not None:
        # Не точное равенство: category_id мастера может быть родительским разделом
        # («Легкові автомобілі»), сами объявления лежат в подразделах-марках.
        if listing.category_id not in categories.descendant_ids(f.category_id):
            return False
    if f.state is not None and listing.state != f.state:
        return False
    if f.owner_type is not None:
        wanted_business = f.owner_type is OwnerType.BUSINESS
        if listing.business != wanted_business:
            return False
    return True


def apply_stop_words(listings: list[Listing], stop_words: list[str]) -> list[Listing]:
    if not stop_words:
        return list(listings)
    # Только title: в описании слишком много ложных срабатываний
    # («не битый», «не является неисправным») -- контракты, раздел Watch.
    needles = [w.lower() for w in stop_words]
    return [lst for lst in listings if not any(n in lst.title.lower() for n in needles)]
