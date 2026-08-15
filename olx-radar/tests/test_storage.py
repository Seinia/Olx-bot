import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from olx import storage
from olx.models import Filters, NotifyMode, OwnerType, PollMode, State
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
    watch = storage.add_watch("iphone 13", _filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
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
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, listing)
    assert _count_rows(db_path, "watch_seen", watch_id=watch.id) == 1

    storage.delete_watch(watch.id)
    assert _count_rows(db_path, "watch_seen", watch_id=watch.id) == 0


def test_update_watch_changes_fields_and_returns_watch(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
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
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    with pytest.raises(ValueError):
        storage.update_watch(watch.id, nonexistent_column="x")


def test_update_watch_missing_id_raises(db_path):
    with pytest.raises(ValueError):
        storage.update_watch(999999, enabled=False)


def test_watch_round_trips_region_id(db_path):
    # Область целиком: city_id пуст, region_id задан -- Filters должен пережить
    # запись/чтение из БД без искажений.
    filters = Filters(category_id=108, region_id=21)
    watch = storage.add_watch("", filters, PollMode.FAST, NotifyMode.NEW, user_id=USER)
    assert watch.filters.city_id is None
    assert watch.filters.region_id == 21

    [reloaded] = storage.list_watches(enabled_only=False)
    assert reloaded.filters.region_id == 21


def test_watch_with_empty_query_round_trips_as_category_only(db_path):
    # Режим «весь раздел без фразы» (см. models.Watch) -- пустая строка, не NULL.
    watch = storage.add_watch(
        "", Filters(category_id=108), PollMode.FAST, NotifyMode.NEW, user_id=USER
    )
    assert watch.query == ""

    [reloaded] = storage.list_watches(enabled_only=False)
    assert reloaded.query == ""
    assert reloaded.filters.category_id == 108


def test_update_watch_can_set_region_id(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
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

    watch = storage.add_watch(
        "q", Filters(region_id=21), PollMode.FAST, NotifyMode.NEW, user_id=USER
    )
    assert watch.filters.region_id == 21


def test_init_db_migrates_a_pre_last_refresh_at_database(tmp_path, sample_listings):
    # Та же миграция, что и для region_id выше, только для watch_seen.last_refresh_at
    # (появилось вместе с детекцией автоподъёма по last_refresh_time).
    path = tmp_path / "legacy2.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY, query TEXT NOT NULL, price_from INTEGER,
                price_to INTEGER, city_id INTEGER, city_name TEXT, region_id INTEGER,
                category_id INTEGER, state TEXT, owner_type TEXT,
                stop_words TEXT NOT NULL DEFAULT '[]', poll_mode TEXT NOT NULL DEFAULT 'fast',
                notify_mode TEXT NOT NULL DEFAULT 'new', enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_polled_at TEXT, last_found_at TEXT
            );
            CREATE TABLE watch_seen (
                watch_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL, last_pushup_at TEXT, notified_at TEXT,
                PRIMARY KEY (watch_id, listing_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db(path)  # не должно упасть
    storage.init_db(path)  # и повторный вызов тоже

    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, dataclasses.replace(listing, refreshed_at=listing.created_at))

    assert storage.last_refresh(watch.id, listing.id) == listing.created_at


def test_stop_words_roundtrip_cyrillic(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    words = ["чохол", "запчастини", "б/у"]
    updated = storage.update_watch(watch.id, stop_words=words)
    assert updated.stop_words == words

    reloaded = storage.list_watches(enabled_only=False)[0]
    assert reloaded.stop_words == words


def test_stop_words_empty_list_roundtrip(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    assert watch.stop_words == []  # дефолт схемы '[]'

    storage.update_watch(watch.id, stop_words=["temp"])
    cleared = storage.update_watch(watch.id, stop_words=[])
    assert cleared.stop_words == []
    assert storage.list_watches(enabled_only=False)[0].stop_words == []


# --- T-07: объявления, снимки цен, дедупликация -----------------------------


def test_seen_ids_empty_before_any_mark(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    assert storage.seen_ids(watch.id) == set()


def test_ac3_repeated_processing_of_same_batch_gives_zero_new(db_path, sample_listings):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
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
    watch_a = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    watch_b = storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
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
    # Первая запись всегда возвращает None: старой цены ещё не было, сравнивать
    # не с чем. Отличить «это первый снимок» от «цена не менялась» по одному
    # только возврату нельзя -- в обоих случаях он None, это ожидаемая неоднозначность.
    assert storage.record_price(listing) is None

    changed = dataclasses.replace(listing, price=(listing.price or 0) + 500)
    storage.upsert_listing(changed)
    # А вот при реальном изменении возвращается именно старая цена -- ей потом
    # оперирует notify._caption() в подписи «было -> стало».
    assert storage.record_price(changed) == listing.price

    assert _count_rows(db_path, "price_snapshots", listing_id=listing.id) == 2


def test_ac7_unchanged_price_adds_no_second_snapshot(db_path, sample_listings):
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    assert storage.record_price(listing) is None  # первый снимок
    assert storage.record_price(listing) is None  # то же самое значение -- снимок не пишется

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
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    assert storage.last_pushup(watch.id, 12345) is None


def test_mark_seen_records_pushup_time(db_path, sample_listings):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    listing = next(x for x in sample_listings if x.pushed_at is not None)
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, listing)

    assert storage.last_pushup(watch.id, listing.id) == listing.pushed_at


def test_last_refresh_returns_none_when_never_seen(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    assert storage.last_refresh(watch.id, 12345) is None


def test_mark_seen_records_refresh_time(db_path, sample_listings):
    # last_refresh_time -- отдельное от pushup_time поле OLX (см. monitor.py):
    # платное продвижение с автоподъёмом двигает именно его, не pushup_time.
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    base = sample_listings[0]
    when = base.created_at
    listing = dataclasses.replace(base, refreshed_at=when)
    storage.upsert_listing(listing)
    storage.mark_seen(watch.id, listing)

    assert storage.last_refresh(watch.id, listing.id) == when


def test_mark_seen_does_not_let_refresh_time_go_backwards(db_path, sample_listings):
    # Тот же защитный принцип, что и у pushup_time (S-07): отставший или пустой
    # last_refresh_time с одного узла не должен затирать более свежий, уже записанный.
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    base = sample_listings[0]
    later = dataclasses.replace(base, refreshed_at=base.created_at)
    storage.upsert_listing(later)
    storage.mark_seen(watch.id, later)

    earlier = dataclasses.replace(base, refreshed_at=None)
    storage.upsert_listing(earlier)
    storage.mark_seen(watch.id, earlier)

    assert storage.last_refresh(watch.id, base.id) == base.created_at


def test_storage_error_before_init_db():
    # Новый модульный процесс -- имитируем "забыли init_db" сбросом состояния.
    storage._db_path = None
    with pytest.raises(storage.StorageError):
        storage.seen_ids(1)


# --- Мультипользовательская изоляция: user_id в watches -- новая колонка, и по
# ней теперь фильтруется вообще всё, что видит владелец запроса в bot.py. Здесь --
# только storage.py; сценарии на уровне команд бота см. в test_bot.py.

USER_A = 111
USER_B = 222


def test_list_watches_scoped_to_user_by_default(db_path):
    storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_B)

    assert [w.query for w in storage.list_watches(user_id=USER_A, enabled_only=False)] == ["a"]
    assert [w.query for w in storage.list_watches(user_id=USER_B, enabled_only=False)] == ["b"]


def test_list_watches_without_user_id_sees_everyone(db_path):
    # user_id=None -- служебный обход для monitor.py: опрашивает всех сразу,
    # а не одного пользователя за раз.
    storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_B)

    everyone = {w.query for w in storage.list_watches(enabled_only=False)}
    assert {"a", "b"} <= everyone


def test_add_watch_records_the_owner(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    assert watch.user_id == USER_A


def test_update_watch_cannot_touch_someone_elses_watch(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    with pytest.raises(ValueError):
        storage.update_watch(watch.id, user_id=USER_B, price_from=5000)

    # И сам запрос не тронут -- storage.update_watch не должен был выполнить
    # никакую часть UPDATE, если владелец не совпал.
    [reloaded] = storage.list_watches(user_id=USER_A, enabled_only=False)
    assert reloaded.filters.price_from is None


def test_update_watch_works_for_the_real_owner(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    updated = storage.update_watch(watch.id, user_id=USER_A, price_from=5000)
    assert updated.filters.price_from == 5000


def test_update_watch_without_user_id_is_unrestricted(db_path):
    # user_id не передан -- обратная совместимость и служебные вызовы (миграции,
    # скрипты): работает как раньше, без проверки владельца.
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    updated = storage.update_watch(watch.id, price_from=5000)
    assert updated.filters.price_from == 5000


def test_delete_watch_cannot_touch_someone_elses_watch(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    storage.delete_watch(watch.id, user_id=USER_B)

    [still_there] = storage.list_watches(user_id=USER_A, enabled_only=False)
    assert still_there.id == watch.id


def test_delete_watch_works_for_the_real_owner(db_path):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    storage.delete_watch(watch.id, user_id=USER_A)

    assert storage.list_watches(user_id=USER_A, enabled_only=False) == []


def test_export_all_only_includes_the_caller_own_finds(db_path, sample_listings):
    watch_a = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    watch_b = storage.add_watch("b", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_B)

    listing_a, listing_b = sample_listings[0], sample_listings[1]
    storage.upsert_listing(listing_a)
    storage.mark_seen(watch_a.id, listing_a)
    storage.upsert_listing(listing_b)
    storage.mark_seen(watch_b.id, listing_b)

    path = storage.export("json", None, user_id=USER_A)
    exported_ids = {row["id"] for row in json.loads(path.read_text(encoding="utf-8"))}

    assert listing_a.id in exported_ids
    assert listing_b.id not in exported_ids


def test_export_specific_watch_id_rejects_a_non_owner(db_path, sample_listings):
    watch_a = storage.add_watch("a", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER_A)
    listing = sample_listings[0]
    storage.upsert_listing(listing)
    storage.mark_seen(watch_a.id, listing)

    # USER_B подставляет чужой watch_id напрямую -- запись должна быть пустой,
    # а не отдать содержимое watch_a (см. JOIN watches ... AND w.user_id = ? в
    # storage._export_rows).
    path = storage.export("json", watch_a.id, user_id=USER_B)
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_init_db_migrates_pre_existing_watches_to_the_default_owner(tmp_path):
    # БД, созданная до появления многопользовательского режима -- ни одна старая
    # запись не должна повиснуть "ничьей" (user_id=0) после апдейта.
    path = tmp_path / "legacy3.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY, query TEXT NOT NULL, price_from INTEGER,
                price_to INTEGER, city_id INTEGER, city_name TEXT, region_id INTEGER,
                category_id INTEGER, state TEXT, owner_type TEXT,
                stop_words TEXT NOT NULL DEFAULT '[]', poll_mode TEXT NOT NULL DEFAULT 'fast',
                notify_mode TEXT NOT NULL DEFAULT 'new', enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_polled_at TEXT, last_found_at TEXT
            );
            CREATE TABLE watch_seen (
                watch_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL, last_pushup_at TEXT, notified_at TEXT,
                PRIMARY KEY (watch_id, listing_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO watches (id, query, created_at) "
            "VALUES (1, 'old', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db(path, default_owner_id=USER_A)

    [migrated] = storage.list_watches(user_id=USER_A, enabled_only=False)
    assert migrated.id == 1
