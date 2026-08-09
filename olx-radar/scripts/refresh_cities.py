"""Собирает справочник городов OLX из выдачи по широким запросам.

Запускать вручную, результат коммитится. Отдельного API у географии нет: geo-encoder
и соседние эндпоинты отвечают общей 404-заглушкой, а в __PRERENDERED_STATE__ лежат
только категории. Зато город приходит в каждом объявлении, и по нескольким широким
запросам набирается вся значимая география: крупные города доминируют по объёму
объявлений и попадают в выборку гарантированно.

    python scripts/refresh_cities.py
"""

import json
import sys
import time
from pathlib import Path

from olx.api import search_raw
from olx.models import Filters

TARGET = Path(__file__).resolve().parents[1] / "data" / "reference" / "olx-cities.json"

# Запросы из разных разделов: недвижимость тянет областные центры, работа и авто --
# райцентры, мебель и телефоны -- всё остальное. Один запрос дал бы перекос географии.
QUERIES = ["квартира", "робота", "телефон", "диван", "авто", "велосипед", "будинок", "ноутбук"]
PAGES = 5
LIMIT = 40
PAUSE = 1.0


def main() -> int:
    cities: dict[int, dict] = {}
    requests_made = 0

    for query in QUERIES:
        for page in range(PAGES):
            try:
                data = search_raw(query, Filters(), offset=page * LIMIT, limit=LIMIT)["data"]
            except Exception as e:
                print(f"  {query} стр.{page}: {type(e).__name__}: {e}", file=sys.stderr)
                break
            requests_made += 1
            if not data:
                break
            for item in data:
                loc = item.get("location") or {}
                city, region = loc.get("city") or {}, loc.get("region") or {}
                if city.get("id") and city.get("name"):
                    cities[city["id"]] = {
                        "id": city["id"],
                        "name": city["name"],
                        "normalized_name": city.get("normalized_name"),
                        "region_id": region.get("id"),
                        "region_name": region.get("name"),
                    }
            time.sleep(PAUSE)
        print(f"  после {query!r}: {len(cities)} городов")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    payload = {str(cid): c for cid, c in sorted(cities.items())}
    TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сохранено {len(cities)} городов за {requests_made} запросов -> {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
