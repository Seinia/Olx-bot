from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

CITIES_PATH = Path(__file__).resolve().parents[2] / "data" / "reference" / "olx-cities.json"

# Порядок осмысленный: сначала то, что ищут чаще всего. Список ручной, потому что
# «крупный город» -- это про население, а справочник OLX его не знает.
MAJOR_CITY_NAMES = (
    "Київ", "Харків", "Одеса", "Дніпро", "Львів", "Запоріжжя",
    "Кривий Ріг", "Миколаїв", "Вінниця", "Полтава", "Чернігів", "Черкаси",
)


@lru_cache(maxsize=1)
def _cities() -> dict[int, dict]:
    if not CITIES_PATH.exists():
        return {}
    return {int(k): v for k, v in json.loads(CITIES_PATH.read_text(encoding="utf-8")).items()}


def city_name(city_id: int) -> str | None:
    city = _cities().get(city_id)
    return city["name"] if city else None


@lru_cache(maxsize=1)
def _regions() -> dict[int, str]:
    # Отдельного справочника областей нет -- region_id/region_name лежат прямо
    # в каждой записи города (см. scripts/refresh_cities.py), собираем на лету.
    out: dict[int, str] = {}
    for city in _cities().values():
        rid, rname = city.get("region_id"), city.get("region_name")
        if rid is not None and rname:
            out[rid] = rname
    return out


def region_name(region_id: int) -> str | None:
    return _regions().get(region_id)


def regions() -> list[tuple[int, str]]:
    """Все области, отсортированы по названию."""
    return sorted(_regions().items(), key=lambda r: r[1])


def find_regions(text: str, *, limit: int = 8) -> list[tuple[int, str]]:
    needle = text.strip().casefold()
    if not needle:
        return []
    starts, contains = [], []
    for rid, name in _regions().items():
        folded = name.casefold()
        if folded.startswith(needle):
            starts.append((rid, name))
        elif needle in folded:
            contains.append((rid, name))
    return (sorted(starts, key=lambda r: r[1]) + sorted(contains, key=lambda r: r[1]))[:limit]


def major_cities() -> list[tuple[int, str]]:
    by_name = {c["name"]: c for c in _cities().values()}
    return [(by_name[n]["id"], n) for n in MAJOR_CITY_NAMES if n in by_name]


def find_cities(text: str, *, limit: int = 8) -> list[tuple[int, str]]:
    """Поиск города по введённому названию.

    Совпадения с начала строки идут первыми: по «Ров» нужен Ровеньки, а не
    Кам'янець-Подільський, где та же тройка букв встретилась в середине.
    """
    needle = text.strip().casefold()
    if not needle:
        return []
    starts, contains = [], []
    for city in _cities().values():
        name = city["name"]
        folded = name.casefold()
        if folded.startswith(needle):
            starts.append((city["id"], name))
        elif needle in folded or needle in (city.get("normalized_name") or ""):
            contains.append((city["id"], name))
    return (sorted(starts, key=lambda c: c[1]) + sorted(contains, key=lambda c: c[1]))[:limit]

def cities_from_raw(raw_items: list[dict], *, limit: int = 8) -> list[tuple[int, str, int]]:
    # Города из выдачи, а не из справочника: geo-encoder и соседние эндпоинты отвечают
    # общей 404-заглушкой (S-05). Зато так в списке только те, где искомое есть.
    counts: Counter[tuple[int, str]] = Counter()
    for item in raw_items:
        city = (item.get("location") or {}).get("city") or {}
        city_id, name = city.get("id"), city.get("name")
        if city_id is not None and name:
            counts[(city_id, name)] += 1
    return [(cid, name, n) for (cid, name), n in counts.most_common(limit)]


MIN_SHARE = 0.05
MIN_COUNT = 2
ALWAYS_KEEP = 3


def categories_from_raw(raw_items: list[dict], *, limit: int = 6) -> list[tuple[int, str, str]]:
    """Разделы, встретившиеся в выдаче, по убыванию частоты: (id, тип, пример заголовка).

    Из выдачи, а не из справочника: списка категорий OLX через API не отдаёт (S-05);
    названия подставляет дерево из categories.py.
    """
    counts: Counter[tuple[int, str]] = Counter()
    examples: dict[int, str] = {}
    for item in raw_items:
        cat = item.get("category") or {}
        cat_id, cat_type = cat.get("id"), cat.get("type") or "?"
        if cat_id is None:
            continue
        counts[(cat_id, cat_type)] += 1
        examples.setdefault(cat_id, item.get("title") or "")

    total = sum(counts.values())
    ranked = counts.most_common(limit)
    # Единичные попадания -- шум (по «Квартира» затесались «Інші послуги» с одним
    # объявлением). Но первые три оставляем всегда: на узком запросе, где всё размазано
    # по чуть-чуть, порог обнулил бы список.
    kept = [
        (cid, ctype, examples.get(cid, ""))
        for i, ((cid, ctype), n) in enumerate(ranked)
        if i < ALWAYS_KEEP or (n >= MIN_COUNT and n / total >= MIN_SHARE)
    ]
    return kept
