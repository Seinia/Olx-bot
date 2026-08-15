import csv
import json
from pathlib import Path

import pytest

from olx import storage
from olx.models import Filters, NotifyMode, PollMode
from olx.parse import parse_search_response

# Тестовый Telegram user_id -- storage.add_watch() теперь мультипользовательский
# и требует владельца; какой именно id, для большинства тестов не важно.
USER = 999999

SAMPLE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample-response.json"


@pytest.fixture(scope="module")
def sample_listings():
    raw = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return parse_search_response(raw)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    storage.init_db(path)
    return path


@pytest.fixture
def exported_file():
    # export() всегда пишет в настоящий data/exports/ (сигнатура фиксирована
    # контрактами -- каталога не подставить), поэтому тест сам убирает за собой.
    paths = []

    def _keep(path):
        paths.append(path)
        return path

    yield _keep
    for path in paths:
        path.unlink(missing_ok=True)


def _seed(n: int, sample_listings) -> list:
    listings = sample_listings[:n]
    for listing in listings:
        storage.upsert_listing(listing)
        storage.record_price(listing)
    return listings


# --- AC-10 ---------------------------------------------------------------


def test_row_count_equals_records_plus_header(db_path, sample_listings, exported_file):
    listings = _seed(5, sample_listings)
    path = exported_file(storage.export("csv"))

    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert len(rows) == len(listings) + 1


def test_csv_starts_with_utf8_bom(db_path, sample_listings, exported_file):
    _seed(1, sample_listings)
    path = exported_file(storage.export("csv"))
    # \xef\xbb\xbf -- сигнатура UTF-8 BOM. Без неё Excel на Windows читает файл как
    # ANSI, и кириллица превращается в кракозябры (AC-10).
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_csv_delimiter_is_semicolon_not_comma(db_path, sample_listings, exported_file):
    _seed(3, sample_listings)
    path = exported_file(storage.export("csv"))
    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    # В украинской/русской локали Excel запятая уже занята под дробную часть числа
    # и по ней столбцы не делятся -- разделитель обязан быть ';'.
    assert ";" in header
    assert header.count(";") == len(storage._EXPORT_FIELDS) - 1


def test_csv_keeps_cyrillic_readable(db_path, sample_listings, exported_file):
    listings = _seed(10, sample_listings)
    path = exported_file(storage.export("csv"))
    text = path.read_text(encoding="utf-8-sig")
    cyrillic_city = next(lst.city_name for lst in listings if lst.city_name)
    assert cyrillic_city in text


def test_json_is_valid_and_not_escaped(db_path, sample_listings, exported_file):
    listings = _seed(4, sample_listings)
    path = exported_file(storage.export("json"))
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert len(data) == len(listings)
    # ensure_ascii=False -- кириллица лежит как есть, а не А... эскейпами.
    assert "\\u" not in text
    cyrillic_city = next(lst.city_name for lst in listings if lst.city_name)
    assert cyrillic_city in text


def test_json_fields_match_listing(db_path, sample_listings, exported_file):
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    storage.record_price(listing)
    path = exported_file(storage.export("json"))
    row = json.loads(path.read_text(encoding="utf-8"))[0]
    assert row["id"] == listing.id
    assert row["url"] == listing.url
    assert row["price"] == listing.price
    assert row["currency"] == listing.currency


# --- watch_id: только то, что показывалось по этому запросу -------------


def test_watch_id_limits_export_to_that_watchs_listings(db_path, sample_listings, exported_file):
    watch_a = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    watch_b = storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)

    seen_by_a, rest = sample_listings[:2], sample_listings[2:5]
    for listing in seen_by_a + rest:
        storage.upsert_listing(listing)
    for listing in seen_by_a:
        storage.mark_seen(watch_a.id, listing)

    path_a = exported_file(storage.export("json", watch_id=watch_a.id))
    ids_a = {row["id"] for row in json.loads(path_a.read_text(encoding="utf-8"))}
    assert ids_a == {lst.id for lst in seen_by_a}

    path_b = exported_file(storage.export("json", watch_id=watch_b.id))
    assert json.loads(path_b.read_text(encoding="utf-8")) == []


def test_no_watch_id_exports_all_listings_regardless_of_watch(
    db_path, sample_listings, exported_file
):
    watch = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    listings = _seed(6, sample_listings)
    storage.mark_seen(watch.id, listings[0])  # только одно попало под watch

    path = exported_file(storage.export("json"))
    ids = {row["id"] for row in json.loads(path.read_text(encoding="utf-8"))}
    assert ids == {lst.id for lst in listings}


def test_export_creates_file_in_exports_dir(db_path, sample_listings, exported_file):
    _seed(1, sample_listings)
    path = exported_file(storage.export("csv"))
    assert path.exists()
    assert path.parent == storage.EXPORTS_DIR


@pytest.mark.telegram
async def test_export_reaches_telegram_via_send_document(db_path, sample_listings, exported_file):
    import asyncio

    from aiogram import Bot
    from aiogram.types import FSInputFile

    from olx.config import settings

    await asyncio.sleep(1)
    _seed(2, sample_listings)
    path = exported_file(storage.export("csv"))

    bot = Bot(settings.telegram_bot_token)
    try:
        await bot.send_document(
            settings.telegram_owner_id,
            FSInputFile(path),
            caption="[ТЕСТ T-16] проверка sendDocument для /export",
        )
    finally:
        await bot.session.close()
