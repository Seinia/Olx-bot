import json
from pathlib import Path

import pytest

from olx import geo, wizard

SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "sample-response.json"
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3000-8000", (3000, 8000)),
        ("3000 - 8000", (3000, 8000)),
        ("3000–8000", (3000, 8000)),
        ("-8000", (None, 8000)),
        ("3000-", (3000, None)),
        ("-", (None, None)),
        ("любая", (None, None)),
        ("5000", (5000, None)),
    ],
)
def test_price_range_forms(text, expected):
    assert wizard.parse_price_range(text) == expected


def test_price_range_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="больше верхней"):
        wizard.parse_price_range("8000-3000")


def test_price_range_rejects_garbage():
    with pytest.raises(ValueError):
        wizard.parse_price_range("дёшево")


def test_stop_words_normalised_and_deduped():
    assert wizard.parse_stop_words("iCloud, на запчасти , ICLOUD") == ["icloud", "на запчасти"]


def test_stop_words_skip():
    assert wizard.parse_stop_words("-") == []


def test_query_validation():
    assert wizard.validate_query("  iphone 13  ") == "iphone 13"
    with pytest.raises(ValueError):
        wizard.validate_query("   ")
    with pytest.raises(ValueError):
        wizard.validate_query("x" * 101)


def test_cities_harvested_from_real_sample():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))["data"]
    cities = geo.cities_from_raw(raw)

    assert cities, "в образце из 52 объявлений города обязаны быть"
    assert all(isinstance(cid, int) and name and n > 0 for cid, name, n in cities)
    assert [n for _, _, n in cities] == sorted((n for _, _, n in cities), reverse=True)
    assert len(cities) <= 8


def test_cities_tolerate_missing_location():
    assert geo.cities_from_raw([{"id": 1}, {"id": 2, "location": {}}]) == []


def test_categories_harvested_with_examples():
    raw = [
        {"id": 1, "category": {"id": 1758, "type": "real_estate"}, "title": "Продам квартиру"},
        {"id": 2, "category": {"id": 1758, "type": "real_estate"}, "title": "Продам житло"},
        {"id": 3, "category": {"id": 1760, "type": "real_estate"}, "title": "Оренда квартири"},
    ]
    cats = geo.categories_from_raw(raw)
    assert cats[0] == (1758, "real_estate", "Продам квартиру")
    assert cats[1] == (1760, "real_estate", "Оренда квартири")


def test_categories_tolerate_missing_category():
    assert geo.categories_from_raw([{"id": 1}, {"id": 2, "category": {}}]) == []


def _item(i, cat_id):
    return {"id": i, "category": {"id": cat_id, "type": "t"}, "title": f"объявление {i}"}


def test_noise_categories_are_dropped():
    # 20 аренды, 15 продажи, 10 подобово и один случайный «Інші послуги».
    items = (
        [_item(i, 1760) for i in range(20)]
        + [_item(100 + i, 1758) for i in range(15)]
        + [_item(200 + i, 3711) for i in range(10)]
        + [_item(999, 186)]
    )
    got = [cid for cid, _, _ in geo.categories_from_raw(items)]
    assert got == [1760, 1758, 3711]


def test_price_presets_split_by_thirds():
    prices = list(range(1000, 21000, 1000))
    presets = wizard.price_presets(prices)

    assert len(presets) == 3
    assert presets[0][1] is None and presets[2][2] is None
    # Пороги должны совпадать: «до X» и «X–Y» без разрыва, иначе часть цен
    # не попадёт ни в одну кнопку.
    assert presets[0][2] == presets[1][1]
    assert presets[1][2] == presets[2][1]


def test_price_presets_rounded_to_readable_numbers():
    presets = wizard.price_presets([12_480 + i * 137 for i in range(30)])
    borders = [presets[0][2], presets[2][1]]
    assert all(b % 100 == 0 for b in borders), borders


def test_price_presets_need_enough_data():
    assert wizard.price_presets([100, 200]) == []
    assert wizard.price_presets([]) == []


def test_price_presets_skipped_when_all_prices_are_equal():
    # Все цены одинаковые -> границы схлопываются, кнопки бессмысленны.
    assert wizard.price_presets([5000] * 20) == []


def test_major_cities_resolve_from_directory():
    majors = dict((name, cid) for cid, name in geo.major_cities())
    assert majors["Київ"] == 268
    assert len(majors) >= 10


def test_find_city_prefers_prefix_matches():
    got = geo.find_cities("Київ")
    assert got and got[0][1] == "Київ"


def test_find_city_is_case_insensitive():
    assert geo.find_cities("харків") == geo.find_cities("ХАРКІВ")


def test_find_city_unknown_returns_nothing():
    assert geo.find_cities("Мордор") == []
    assert geo.find_cities("   ") == []


def test_city_name_lookup():
    assert geo.city_name(268) == "Київ"
    assert geo.city_name(99_999_999) is None


def test_top_three_survive_even_when_everything_is_thin():
    # Узкий запрос: всё размазано по одному объявлению. Отсекать тут нечего,
    # иначе список окажется пустым и выбирать будет не из чего.
    items = [_item(i, 1000 + i) for i in range(5)]
    assert len(geo.categories_from_raw(items)) == 3


@pytest.mark.network
def test_category_id_really_filters():
    """Тот же класс проверки, что для owner_type: 200 OK ещё не значит, что фильтр работает."""
    from olx.api import search_raw
    from olx.models import Filters

    for category_id in (1758, 1760):
        data = search_raw("квартира", Filters(category_id=category_id))["data"]
        assert data
        assert {item["category"]["id"] for item in data} == {category_id}
