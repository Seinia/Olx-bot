from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from olx import categories
from olx.errors import StorageError
from olx.models import Filters, Listing, NotifyMode, OwnerType, PollMode, State, Watch

# Не data/olx.db -- выгрузки одноразовые файлы для владельца, держать их рядом
# с боевой БД незачем, а чистить их отдельно от бэкапов БД удобнее.
EXPORTS_DIR = Path(__file__).resolve().parents[2] / "data" / "exports"

# Схема дословно из контрактов (docs/pipeline/contracts.md, раздел 2).
# IF NOT EXISTS добавлен относительно контракта: init_db() вызывается при каждом
# старте демона на постоянный data/olx.db, а не только один раз при разворачивании --
# без него второй запуск падал бы на "table already exists".
_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY,
    query           TEXT    NOT NULL,
    price_from      INTEGER,
    price_to        INTEGER,
    city_id         INTEGER,
    city_name       TEXT,
    region_id       INTEGER,
    category_id     INTEGER,
    state           TEXT,
    owner_type      TEXT,
    -- stop_words: JSON-массив; отдельная таблица избыточна при 1-5 запросах
    stop_words      TEXT    NOT NULL DEFAULT '[]',
    poll_mode       TEXT    NOT NULL DEFAULT 'fast',
    notify_mode     TEXT    NOT NULL DEFAULT 'new',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    last_polled_at  TEXT,
    last_found_at   TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,      -- id объявления OLX
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    city_id       INTEGER,
    city_name     TEXT,
    region_id     INTEGER,
    business      INTEGER NOT NULL,
    state         TEXT,
    category_id   INTEGER,
    seller_id     INTEGER,
    seller_name   TEXT,
    photo_url     TEXT,
    created_at    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    price      INTEGER,
    currency   TEXT,
    negotiable INTEGER NOT NULL DEFAULT 0,
    seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_listing ON price_snapshots(listing_id, seen_at);

CREATE TABLE IF NOT EXISTS watch_seen (
    watch_id       INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    listing_id     INTEGER NOT NULL REFERENCES listings(id),
    first_seen_at  TEXT    NOT NULL,
    last_pushup_at TEXT,
    notified_at    TEXT,
    PRIMARY KEY (watch_id, listing_id)
);
"""

# Публичные функции не принимают connection/path (см. сигнатуры в контрактах) --
# единственный способ их связать без протаскивания параметра через весь модуль.
_db_path: Path | None = None


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    global _db_path
    _db_path = path
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        # Миграция для БД, созданных до появления region_id: CREATE TABLE IF NOT
        # EXISTS новую колонку в старую таблицу не добавляет. OperationalError
        # значит «колонка уже есть» -- это ожидаемо на каждом повторном запуске.
        try:
            conn.execute("ALTER TABLE watches ADD COLUMN region_id INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise StorageError("storage.init_db() ещё не вызван")
    try:
        # /export, ручной sqlite3 и бэкап держат файл снаружи процесса --
        # дефолтных 5 с ожидания не хватало.
        conn = sqlite3.connect(_db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error as e:
        raise StorageError(f"БД недоступна: {e}") from e
    conn.row_factory = sqlite3.Row
    # SQLite не проверяет внешние ключи по умолчанию -- без этого ON DELETE CASCADE
    # у watch_seen молча не сработает.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    except sqlite3.Error as e:
        conn.rollback()
        raise StorageError(f"Ошибка БД: {e}") from e
    finally:
        conn.close()


def _opt_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_watch(row: sqlite3.Row) -> Watch:
    filters = Filters(
        price_from=row["price_from"],
        price_to=row["price_to"],
        city_id=row["city_id"],
        region_id=row["region_id"],
        category_id=row["category_id"],
        state=State(row["state"]) if row["state"] else None,
        owner_type=OwnerType(row["owner_type"]) if row["owner_type"] else None,
    )
    return Watch(
        id=row["id"],
        query=row["query"],
        filters=filters,
        stop_words=json.loads(row["stop_words"]),
        poll_mode=PollMode(row["poll_mode"]),
        notify_mode=NotifyMode(row["notify_mode"]),
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_polled_at=_opt_datetime(row["last_polled_at"]),
        last_found_at=_opt_datetime(row["last_found_at"]),
    )


def add_watch(query: str, filters: Filters, poll_mode: PollMode, notify_mode: NotifyMode) -> Watch:
    # stop_words и city_name сюда намеренно не входят: Filters их не несёт (контракты,
    # раздел Watch), а city_name сейчас некому заполнить -- город известен только по
    # id (S-05, открытый вопрос 1). Оба дополняются позже через update_watch().
    with _connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO watches (query, price_from, price_to, city_id, region_id, category_id,
                                  state, owner_type, poll_mode, notify_mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query,
                filters.price_from,
                filters.price_to,
                filters.city_id,
                filters.region_id,
                filters.category_id,
                filters.state.value if filters.state else None,
                filters.owner_type.value if filters.owner_type else None,
                poll_mode.value,
                notify_mode.value,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_watch(row)


def list_watches(*, enabled_only: bool = True) -> list[Watch]:
    sql = "SELECT * FROM watches"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    with _connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_watch(row) for row in rows]


def delete_watch(watch_id: int) -> None:
    with _connection() as conn:
        conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        conn.commit()


# Белый список защищает от произвольных имён колонок в динамическом UPDATE ниже --
# **fields идёт из вызывающего кода (бот), доверять именам ключей напрямую нельзя.
_WATCH_COLUMNS = {
    "query", "price_from", "price_to", "city_id", "city_name", "region_id", "category_id",
    "state", "owner_type",
    "stop_words", "poll_mode", "notify_mode", "enabled", "last_polled_at", "last_found_at",
}


def _serialize_watch_field(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "stop_words":
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if name == "enabled":
        return int(value)
    return value


def update_watch(watch_id: int, **fields: Any) -> Watch:
    unknown = set(fields) - _WATCH_COLUMNS
    if unknown:
        raise ValueError(f"update_watch: неизвестные поля {sorted(unknown)}")
    with _connection() as conn:
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = [_serialize_watch_field(name, value) for name, value in fields.items()]
            conn.execute(f"UPDATE watches SET {assignments} WHERE id = ?", (*values, watch_id))
            conn.commit()
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    if row is None:
        raise ValueError(f"watch {watch_id} не найден")
    return _row_to_watch(row)


def upsert_listing(listing: Listing) -> None:
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO listings (id, url, title, city_id, city_name, region_id, business,
                                   state, category_id, seller_id, seller_name, photo_url,
                                   created_at, first_seen_at)
            VALUES (:id, :url, :title, :city_id, :city_name, :region_id, :business,
                    :state, :category_id, :seller_id, :seller_name, :photo_url,
                    :created_at, :first_seen_at)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url, title = excluded.title, city_id = excluded.city_id,
                city_name = excluded.city_name, region_id = excluded.region_id,
                business = excluded.business, state = excluded.state,
                category_id = excluded.category_id, seller_id = excluded.seller_id,
                seller_name = excluded.seller_name, photo_url = excluded.photo_url,
                created_at = excluded.created_at
            """,
            {
                "id": listing.id,
                "url": listing.url,
                "title": listing.title,
                "city_id": listing.city_id,
                "city_name": listing.city_name,
                "region_id": listing.region_id,
                "business": int(listing.business),
                "state": listing.state.value if listing.state else None,
                "category_id": listing.category_id,
                "seller_id": listing.seller_id,
                "seller_name": listing.seller_name,
                "photo_url": listing.photo_url,
                "created_at": listing.created_at.isoformat(),
                # Не входит в DO UPDATE SET -- на конфликте это значение отбрасывается,
                # а не затирает первую встречу объявления при повторном upsert.
                "first_seen_at": datetime.now(UTC).isoformat(),
            },
        )
        conn.commit()


def record_price(listing: Listing) -> int | None:
    with _connection() as conn:
        last = conn.execute(
            "SELECT price, currency "
            "FROM price_snapshots "
            "WHERE listing_id = ? "
            "ORDER BY seen_at DESC, id DESC LIMIT 1",
            (listing.id,),
        ).fetchone()

        if last is not None:
            if (
                last["price"] == listing.price
                and last["currency"] == listing.currency
            ):
                return None

        old_price = last["price"] if last is not None else None

        conn.execute(
            "INSERT INTO price_snapshots "
            "(listing_id, price, currency, negotiable, seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                listing.id,
                listing.price,
                listing.currency,
                int(listing.negotiable),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

        return old_price

def mark_seen(watch_id: int, listing: Listing) -> None:
    with _connection() as conn:
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT last_pushup_at FROM watch_seen WHERE watch_id = ? AND listing_id = ?",
            (watch_id, listing.id),
        ).fetchone()
        previous = _opt_datetime(row["last_pushup_at"]) if row is not None else None

        # last_pushup_at может только расти: узел с отставшим или пустым pushup_time
        # (S-07) затирал актуальное, и объявление уведомляло по кругу.
        pushed = listing.pushed_at
        if previous is not None and (pushed is None or pushed <= previous):
            pushed = previous

        conn.execute(
            """
            INSERT INTO watch_seen
                (watch_id, listing_id, first_seen_at, last_pushup_at, notified_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(watch_id, listing_id)
                DO UPDATE SET last_pushup_at = excluded.last_pushup_at,
                              notified_at    = excluded.notified_at
            """,
            (watch_id, listing.id, now, pushed.isoformat() if pushed else None, now),
        )
        conn.commit()


def seen_ids(watch_id: int) -> set[int]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT listing_id FROM watch_seen WHERE watch_id = ?", (watch_id,)
        ).fetchall()
    return {row["listing_id"] for row in rows}


def last_pushup(watch_id: int, listing_id: int) -> datetime | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT last_pushup_at FROM watch_seen WHERE watch_id = ? AND listing_id = ?",
            (watch_id, listing_id),
        ).fetchone()
    return _opt_datetime(row["last_pushup_at"]) if row is not None else None


# Порядок и подписи полей выгрузки -- одни для CSV и JSON (контракты требуют только
# "полезные поля", формат подписи не оговорён). Подписи русские: файл открывает
# владелец, а не код.
_EXPORT_FIELDS: list[tuple[str, str]] = [
    ("id", "ID"),
    ("url", "Ссылка"),
    ("title", "Заголовок"),
    ("price", "Цена"),
    ("currency", "Валюта"),
    ("city_name", "Город"),
    ("category", "Раздел"),
    ("owner_type", "Продавец"),
    ("created_at", "Опубликовано"),
    ("first_seen_at", "Впервые увидели"),
    ("last_pushup_at", "Последний раз поднято")
]

# Та же подзапросная развязка id DESC, что и в record_price -- при опросе раз
# в 20-30 с два снимка могут лечь в одну секунду системных часов.
_LATEST_PRICE_ID = """
    (SELECT id FROM price_snapshots WHERE listing_id = l.id ORDER BY seen_at DESC, id DESC LIMIT 1)
"""


def _export_rows(watch_id: int | None) -> list[dict[str, Any]]:
    # watch_id задан -- берём watch_seen.first_seen_at: это "когда эту вещь увидел
    # именно этот запрос", а не listings.first_seen_at, который мог набежать раньше
    # от другого запроса на ту же вещь (дедуп в contracts.md намеренно попарный).
    if watch_id is not None:
        sql = f"""
            SELECT l.id, l.url, l.title, l.city_name, l.category_id, l.business, l.created_at,
                   ws.first_seen_at, ws.last_pushup_at, p.price, p.currency
            FROM listings l
            JOIN watch_seen ws ON ws.listing_id = l.id AND ws.watch_id = ?
            LEFT JOIN price_snapshots p ON p.id = {_LATEST_PRICE_ID}
            ORDER BY l.id
        """
        params: tuple[Any, ...] = (watch_id,)
    else:
        sql = f"""
            SELECT l.id, l.url, l.title, l.city_name, l.category_id, l.business, l.created_at,
                   l.first_seen_at, NULL AS last_pushup_at, p.price, p.currency
            FROM listings l
            LEFT JOIN price_snapshots p ON p.id = {_LATEST_PRICE_ID}
            ORDER BY l.id
        """
        params = ()
    with _connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row["id"],
            "url": row["url"],
            "title": row["title"],
            "price": row["price"],
            "currency": row["currency"],
            "city_name": row["city_name"],
            "category": categories.label(row["category_id"]) if row["category_id"] else "",
            "owner_type": "бизнес" if row["business"] else "частник",
            "created_at": row["created_at"],
            "first_seen_at": row["first_seen_at"],
            "last_pushup_at": row["last_pushup_at"]
        }
        for row in rows
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    # utf-8-sig, не utf-8: без BOM Excel на Windows определяет кодировку файла как
    # ANSI, и кириллица превращается в кракозябры (AC-10). ';', не ',' -- в
    # украинской/русской локали Excel запятая уже занята под дробную часть числа
    # и по ней столбцы не разбиваются.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header for _, header in _EXPORT_FIELDS)
        for row in rows:
            writer.writerow(row[key] for key, _ in _EXPORT_FIELDS)


def _write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def export(fmt: Literal["csv", "json"], watch_id: int | None = None) -> Path:
    rows = _export_rows(watch_id)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    scope = f"watch{watch_id}" if watch_id is not None else "all"
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = EXPORTS_DIR / f"{scope}_{stamp}.{fmt}"
    if fmt == "csv":
        _write_csv(path, rows)
    else:
        _write_json(path, rows)
    return path


def purge_old_history(*, retention_days: int, now: datetime | None = None) -> dict[str, int]:
    """Удаляет то, что уже не нужно хранить, и возвращает счётчики.

    «Ушедшее с OLX» = max(watch_seen.notified_at) старше retention; notified_at
    обновляется на каждом опросе, поэтому это «когда видели в последний раз». Такое
    объявление удаляем целиком (вернётся через месяц -- уведомим заново, это новое
    событие). У живых чистим снимки старше retention, кроме последнего: по нему
    record_price ловит изменение цены.
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    with _connection() as conn:
        stale = [
            row["id"]
            for row in conn.execute(
                """
                SELECT l.id FROM listings l
                WHERE (SELECT MAX(ws.notified_at) FROM watch_seen ws
                       WHERE ws.listing_id = l.id) < ?
                """,
                (cutoff,),
            ).fetchall()
        ]
        # Порядок из-за foreign_keys=ON: сначала зависимые строки, потом сами listings.
        params = [(listing_id,) for listing_id in stale]
        conn.executemany("DELETE FROM price_snapshots WHERE listing_id = ?", params)
        conn.executemany("DELETE FROM watch_seen WHERE listing_id = ?", params)
        conn.executemany("DELETE FROM listings WHERE id = ?", params)

        # id автоинкрементный и растёт вместе с seen_at, поэтому MAX(id) на объявление
        # -- это его самый свежий снимок.
        pruned = conn.execute(
            """
            DELETE FROM price_snapshots
            WHERE seen_at < ?
              AND id NOT IN (SELECT MAX(id) FROM price_snapshots GROUP BY listing_id)
            """,
            (cutoff,),
        ).rowcount
        conn.commit()
    return {"listings": len(stale), "snapshots": pruned}
