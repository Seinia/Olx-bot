from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class PollMode(StrEnum):
    FAST = "fast"
    FULL = "full"


class NotifyMode(StrEnum):
    NEW = "new"
    NEW_PRICE = "new_price"
    NEW_PUSHUP = "new_pushup"


class State(StrEnum):
    NEW = "new"
    USED = "used"


class OwnerType(StrEnum):
    PRIVATE = "private"
    BUSINESS = "business"


@dataclass(frozen=True)
class Listing:
    id: int
    url: str
    title: str
    description: str
    price: int | None
    currency: str | None
    negotiable: bool
    arranged: bool
    city_id: int | None
    city_name: str
    region_id: int | None
    business: bool
    state: State | None
    status: str
    created_at: datetime
    pushed_at: datetime | None
    refreshed_at: datetime | None
    photo_url: str | None
    category_id: int | None
    seller_id: int | None
    seller_name: str
    # Первая ссылка остаётся отдельным полем для обратной совместимости с БД,
    # полный набор нужен для альбома в Telegram.
    photo_urls: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class Filters:
    """Фильтры, исполняемые сервером OLX — уходят в параметры запроса."""

    price_from: int | None = None
    price_to: int | None = None
    city_id: int | None = None
    # Вся область целиком, без привязки к конкретному городу. Взаимоисключающе
    # с city_id: мастер /add не даёт задать оба сразу (см. bot.py, _ask_city).
    region_id: int | None = None
    # Различает подкатегории внутри одного поискового слова: «квартира» попадает
    # и в продажу (1758), и в аренду (1760), и в услуги риелторов (186).
    category_id: int | None = None
    state: State | None = None
    owner_type: OwnerType | None = None


@dataclass
class Watch:
    id: int
    # Пустая строка -- запрос без поисковой фразы, «весь раздел» целиком
    # (filters.category_id тогда обязателен, см. wizard.py/bot.py).
    query: str
    filters: Filters
    # Постфильтр: в API такого параметра нет, отсекаем у себя после разбора ответа.
    # Держится отдельно от Filters, чтобы граница «серверное / наше» была видна в типах.
    stop_words: list[str] = field(default_factory=list)
    poll_mode: PollMode = PollMode.FAST
    notify_mode: NotifyMode = NotifyMode.NEW
    enabled: bool = True
    created_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_found_at: datetime | None = None
