import copy
import dataclasses
import json
from pathlib import Path

import pytest

from olx.errors import SchemaError
from olx.parse import apply_stop_words, parse_listing, parse_search_response

SAMPLE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample-response.json"


@pytest.fixture(scope="module")
def sample_raw():
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sample_listings(sample_raw):
    return parse_search_response(sample_raw)


def test_all_52_sample_listings_parse(sample_listings):
    assert len(sample_listings) == 52


def test_each_raw_item_parses_individually(sample_raw):
    # parse_search_response уже это гоняет, но DoD требует проверки поштучно.
    for item in sample_raw["data"]:
        parse_listing(item)


def test_price_extracted_from_params(sample_listings):
    listing = next(x for x in sample_listings if x.id == 928002010)
    assert listing.price == 15100
    assert listing.currency == "UAH"
    assert listing.negotiable is False
    assert listing.arranged is False


def test_state_extracted_from_params(sample_listings):
    listing = next(x for x in sample_listings if x.id == 928002010)
    assert listing.state == "used"


def test_photo_url_has_no_placeholders(sample_listings):
    with_photos = [x for x in sample_listings if x.photo_url is not None]
    assert with_photos  # в образце у всех 52 есть фото -- проверка не тривиальна
    for listing in with_photos:
        assert "{width}" not in listing.photo_url
        assert "{height}" not in listing.photo_url
        assert "800x600" in listing.photo_url


def test_business_from_top_level_not_from_user_seller_type(sample_raw):
    # user.seller_type == None у всех 52 в образце (S-01+02) -- если бы парсер
    # читал его, business получился бы не тем, что реально в JSON.
    assert all(item["user"]["seller_type"] is None for item in sample_raw["data"])
    listings = parse_search_response(sample_raw)
    business_count = sum(1 for x in listings if x.business)
    assert business_count == sum(1 for item in sample_raw["data"] if item["business"])
    assert business_count == 11


def test_missing_data_key_raises_schema_error():
    with pytest.raises(SchemaError):
        parse_search_response({"metadata": {}, "links": {}})


def test_data_not_a_list_raises_schema_error():
    with pytest.raises(SchemaError):
        parse_search_response({"data": {"id": 1}})


def test_listing_without_id_raises_schema_error(sample_raw):
    broken = copy.deepcopy(sample_raw["data"][0])
    del broken["id"]
    with pytest.raises(SchemaError):
        parse_listing(broken)


def test_unparseable_created_time_raises_schema_error(sample_raw):
    broken = copy.deepcopy(sample_raw["data"][0])
    broken["created_time"] = "не дата"
    with pytest.raises(SchemaError):
        parse_listing(broken)


def test_corrupted_item_in_batch_raises_not_silently_drops(sample_raw):
    # Одно битое объявление в выдаче не должно превращаться в "51 из 52 без предупреждения".
    broken_batch = copy.deepcopy(sample_raw)
    del broken_batch["data"][3]["id"]
    with pytest.raises(SchemaError):
        parse_search_response(broken_batch)


def test_stop_words_filter_by_title_case_insensitive(sample_listings):
    # Обе стороны: до фильтра "чохол" в наборе есть, после -- нет ни одного.
    before = [x for x in sample_listings if "чохол" in x.title.lower()]
    assert len(before) == 11

    after = apply_stop_words(sample_listings, ["ЧОХОЛ"])
    assert len(after) == len(sample_listings) - 11
    assert all("чохол" not in x.title.lower() for x in after)


def test_stop_words_do_not_check_description(sample_listings):
    # Слово только в description, не в title -- контракты явно исключают description
    # из-за ложных срабатываний вида "не битый". Проверяем это поведение напрямую,
    # а не полагаемся на то, что нужное слово случайно найдётся в образце.
    listing = dataclasses.replace(
        sample_listings[0],
        title="Iphone 13 Pro Max 512 gb",
        description="Экран не битый, трещин нет",
    )
    result = apply_stop_words([listing], ["битый"])
    assert result == [listing]


def test_apply_stop_words_empty_list_is_noop(sample_listings):
    assert apply_stop_words(sample_listings, []) == sample_listings
