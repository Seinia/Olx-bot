"""Обновляет справочник категорий OLX из главной страницы сайта.

Запускать вручную, когда OLX перекроит дерево разделов: результат коммитится.
Отдельного API у категорий нет — `/api/v1/categories/` и соседние отдают 404, а реестр
фильтров возвращает один и тот же глобальный список независимо от category_id.
Зато дерево целиком лежит в `window.__PRERENDERED_STATE__` любой страницы olx.ua.

    python scripts/refresh_categories.py
"""

import json
import re
import sys
from pathlib import Path

from curl_cffi import requests

SOURCE = "https://www.olx.ua/uk/"
TARGET = Path(__file__).resolve().parents[1] / "data" / "reference" / "olx-categories.json"

# Из узла берём только то, что нужно для навигации: полный дамп — это 2385 записей
# с иконками, порядком сортировки и параметрами загрузки фото.
KEEP = ("id", "name", "parentId", "level", "normalizedName", "isSearch")


def main() -> int:
    html = requests.get(SOURCE, impersonate="chrome", timeout=30).text
    match = re.search(r"window\.__PRERENDERED_STATE__\s*=\s*(\".*?\")\s*;?\s*\n", html, re.S)
    if not match:
        print("На странице нет __PRERENDERED_STATE__ — разметка OLX изменилась.", file=sys.stderr)
        return 1

    raw = json.loads(json.loads(match.group(1)))["categories"]["list"]
    tree = {}
    for key, node in raw.items():
        children = [c if isinstance(c, int) else c.get("id") for c in (node.get("children") or [])]
        tree[key] = {k: node.get(k) for k in KEEP} | {"children": children}

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(tree, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    roots = sum(1 for n in tree.values() if not n.get("parentId"))
    print(f"Сохранено {len(tree)} категорий ({roots} корневых) -> {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
