import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from olx import storage
from olx.models import Filters, NotifyMode, OwnerType, PollMode, State
from olx.parse import parse_search_response

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


def _filters() -> Filters:
    return Filters(
        price_from=3000, price_to=8000, city_id=268, state=State.USED, owner_type=OwnerType.PRIVATE
    )


def _count_rows(db_path, table: str, **where) -> int:
    conn = sqlite3.connect(db_path)
    try:
        clause = " AND ".join(f"{k} = ?" for k in where)
        sql = f"SELECT COUNT(*) FROM {table}"
        if clause:
            sql += f" WHERE {clause}"
        return conn.execute(sql, tuple(where.values())).fetchone()[0]
    finally:
        conn.close()


# --- T-06: схема и CRUD по watches -----------------------------------------


def test_init_db_creates_all_four_tables(tmp_path):
    path = tmp_path / "fresh.db"
    storage.init_db(path)
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"watches", "listings", "price_snapshots", "watch_seen"} <= tables


def test_init_db_is_idempotent(tmp_path):
    # Демон вызывает init_db() при каждом старте на постоянный файл -- второй вызов
    # не должен падать на "table already exists".
    path = tmp_path / "fresh.db"
    storage.init_db(path)
    storage.init_db(path)


def test_add_list_delete_watch(db_path):
    watch = storage.add_watch("iphone 13", _filters(), PollMode.FAST, NotifyMode.NEW)
    assert watch.query == "iphone 13"
    assert watch.filters == _filters()
    assert watch.stop_words == []
    assert watch.poll_mode == PollMode.FAST
    assert watch.notify_mode == NotifyMode.NEW
    assert watch.enabled is True
    assert watch.created_at is not None

    assert storage.list_watches() == [watch]

    storage.delete_watch(watch.id)
    assert storage.list_watches(enabled_only=False) == []


def test_delete_watch_cascades_watch_seen(db_path, sample_listings):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, listing)
    assert _count_rows(db_path, "watch_seen", watch_id=watch.id) == 1

    storage.delete_watch(watch.id)
    assert _count_rows(db_path, "watch_seen", watch_id=watch.id) == 0


def test_update_watch_changes_fields_and_returns_watch(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    updated = storage.update_watch(
        watch.id, poll_mode=PollMode.FULL, notify_mode=NotifyMode.NEW_PUSHUP, enabled=False
    )
    assert updated.poll_mode == PollMode.FULL
    assert updated.notify_mode == NotifyMode.NEW_PUSHUP
    assert updated.enabled is False

    # list_watches(enabled_only=True) по умолчанию скрывает выключенные -- проверяем,
    # что update действительно попал в БД, а не только в возвращённый объект.
    assert storage.list_watches() == []
    assert storage.list_watches(enabled_only=False) == [updated]


def test_update_watch_unknown_field_rejected(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    with pytest.raises(ValueError):
        storage.update_watch(watch.id, nonexistent_column="x")


def test_update_watch_missing_id_raises(db_path):
    with pytest.raises(ValueError):
        storage.update_watch(999999, enabled=False)


def test_watch_round_trips_region_id(db_path):
    # Область целиком: city_id пуст, region_id задан -- Filters должен пережить
    # запись/чтение из БД без искажений.
    filters = Filters(category_id=108, region_id=21)
    watch = storage.add_watch("", filters, PollMode.FAST, NotifyMode.NEW)
    assert watch.filters.city_id is None
    assert watch.filters.region_id == 21

    [reloaded] = storage.list_watches(enabled_only=False)
    assert reloaded.filters.region_id == 21


def test_watch_with_empty_query_round_trips_as_category_only(db_path):
    # Режим «весь раздел без фразы» (см. models.Watch) -- пустая строка, не NULL.
    watch = storage.add_watch("", Filters(category_id=108), PollMode.FAST, NotifyMode.NEW)
    assert watch.query == ""

    [reloaded] = storage.list_watches(enabled_only=False)
    assert reloaded.query == ""
    assert reloaded.filters.category_id == 108


def test_update_watch_can_set_region_id(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    updated = storage.update_watch(watch.id, region_id=5)
    assert updated.filters.region_id == 5


def test_init_db_migrates_a_pre_region_id_database(tmp_path):
    # БД, созданная до появления region_id, не должна падать при следующем
    # запуске демона -- ALTER TABLE в init_db() обязан отработать один раз и молчать
    # на повторных вызовах (OperationalError "duplicate column" — ожидаемо, не ошибка).
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY, query TEXT NOT NULL, price_from INTEGER,
                price_to INTEGER, city_id INTEGER, city_name TEXT, category_id INTEGER,
                state TEXT, owner_type TEXT, stop_words TEXT NOT NULL DEFAULT '[]',
                poll_mode TEXT NOT NULL DEFAULT 'fast', notify_mode TEXT NOT NULL DEFAULT 'new',
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                last_polled_at TEXT, last_found_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db(path)  # не должно упасть
    storage.init_db(path)  # и повторный вызов тоже

    watch = storage.add_watch("q", Filters(region_id=21), PollMode.FAST, NotifyMode.NEW)
    assert watch.filters.region_id == 21


def test_stop_words_roundtrip_cyrillic(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    words = ["чохол", "запчастини", "б/у"]
    updated = storage.update_watch(watch.id, stop_words=words)
    assert updated.stop_words == words

    reloaded = storage.list_watches(enabled_only=False)[0]
    assert reloaded.stop_words == words


def test_stop_words_empty_list_roundtrip(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    assert watch.stop_words == []  # дефолт схемы '[]'

    storage.update_watch(watch.id, stop_words=["temp"])
    cleared = storage.update_watch(watch.id, stop_words=[])
    assert cleared.stop_words == []
    assert storage.list_watches(enabled_only=False)[0].stop_words == []


# --- T-07: объявления, снимки цен, дедупликация -----------------------------


def test_seen_ids_empty_before_any_mark(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    assert storage.seen_ids(watch.id) == set()


def test_ac3_repeated_processing_of_same_batch_gives_zero_new(db_path, sample_listings):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    all_ids = {listing.id for listing in sample_listings}

    for listing in sample_listings:
        storage.upsert_listing(listing)
        storage.mark_seen(watch.id, listing)

    assert storage.seen_ids(watch.id) == all_ids

    # Второй прогон по тому же набору не должен упасть на PK-конфликте
    # (watch_id, listing_id) и не должен прибавить ни одного "нового" id.
    for listing in sample_listings:
        storage.upsert_listing(listing)
        storage.mark_seen(watch.id, listing)

    assert storage.seen_ids(watch.id) == all_ids
    assert all_ids - storage.seen_ids(watch.id) == set()


def test_dedup_is_per_watch_listing_pair_not_global(db_path, sample_listings):
    # Два запроса пересекаются по выдаче -- оба обязаны уведомить. Глобальный дедуп
    # по listing_id молча съел бы второе уведомление (контракты, раздел 2).
    watch_a = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW)
    watch_b = storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW)
    listing = sample_listings[0]
    storage.upsert_listing(listing)

    storage.mark_seen(watch_a.id, listing)
    assert listing.id in storage.seen_ids(watch_a.id)
    assert listing.id not in storage.seen_ids(watch_b.id)

    storage.mark_seen(watch_b.id, listing)
    assert listing.id in storage.seen_ids(watch_b.id)


def test_upsert_listing_preserves_first_seen_at_on_conflict(db_path, sample_listings):
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    first_seen_before = _count_rows(db_path, "listings", id=listing.id)
    assert first_seen_before == 1

    conn = sqlite3.connect(db_path)
    try:
        original_first_seen = conn.execute(
            "SELECT first_seen_at FROM listings WHERE id = ?", (listing.id,)
        ).fetchone()[0]
    finally:
        conn.close()

    renamed = dataclasses.replace(listing, title="Другое название")
    storage.upsert_listing(renamed)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT title, first_seen_at FROM listings WHERE id = ?", (listing.id,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "Другое название"
    assert row[1] == original_first_seen
    assert _count_rows(db_path, "listings", id=listing.id) == 1


def test_ac7_price_change_adds_second_snapshot(db_path, sample_listings):
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    assert storage.record_price(listing) is True

    changed = dataclasses.replace(listing, price=(listing.price or 0) + 500)
    storage.upsert_listing(changed)
    assert storage.record_price(changed) is True

    assert _count_rows(db_path, "price_snapshots", listing_id=listing.id) == 2


def test_ac7_unchanged_price_adds_no_second_snapshot(db_path, sample_listings):
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    assert storage.record_price(listing) is True
    assert storage.record_price(listing) is False  # то же самое значение -- снимок не пишется

    assert _count_rows(db_path, "price_snapshots", listing_id=listing.id) == 1


def test_ac7_two_consecutive_runs_do_not_double_price_snapshots(db_path, sample_listings):
    # При опросе раз в 25 с наивная запись "на каждую встречу" дала бы рост таблицы
    # линейно с числом прогонов -- контракты явно требуют обратного.
    for listing in sample_listings:
        storage.upsert_listing(listing)
        storage.record_price(listing)

    first_pass_total = _count_rows(db_path, "price_snapshots")
    assert first_pass_total == len(sample_listings)

    for listing in sample_listings:
        storage.upsert_listing(listing)
        storage.record_price(listing)

    assert _count_rows(db_path, "price_snapshots") == first_pass_total


def test_last_pushup_returns_none_when_never_seen(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    assert storage.last_pushup(watch.id, 12345) is None


def test_mark_seen_records_pushup_time(db_path, sample_listings):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    listing = next(x for x in sample_listings if x.pushed_at is not None)
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, listing)

    assert storage.last_pushup(watch.id, listing.id) == listing.pushed_at


def test_storage_error_before_init_db():
    # Новый модульный процесс -- имитируем "забыли init_db" сбросом состояния.
    storage._db_path = None
    with pytest.raises(storage.StorageError):
        storage.seen_ids(1)
