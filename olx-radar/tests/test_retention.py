import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olx import monitor, storage
from olx.models import Filters, NotifyMode, PollMode
from olx.monitor import Poller
from olx.parse import parse_listing

# Тестовый Telegram user_id -- storage.add_watch() теперь мультипользовательский
# и требует владельца; какой именно id, для большинства тестов не важно.
USER = 999999

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample-response.json"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    storage.init_db(tmp_path / "retention.db")


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))["data"]


def _set_notified(listing_id: int, when: datetime) -> None:
    conn = sqlite3.connect(storage._db_path)
    conn.execute(
        "UPDATE watch_seen SET notified_at = ? WHERE listing_id = ?",
        (when.isoformat(), listing_id),
    )
    conn.commit()
    conn.close()


def _add_snapshot(listing_id: int, price: int, seen_at: datetime) -> None:
    conn = sqlite3.connect(storage._db_path)
    conn.execute(
        "INSERT INTO price_snapshots (listing_id, price, currency, negotiable, seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (listing_id, price, "UAH", 0, seen_at.isoformat()),
    )
    conn.commit()
    conn.close()


def _prices(listing_id: int) -> list[int]:
    conn = sqlite3.connect(storage._db_path)
    rows = conn.execute(
        "SELECT price FROM price_snapshots WHERE listing_id = ? ORDER BY id", (listing_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_purge_deletes_listing_gone_beyond_retention(db, sample):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    gone = parse_listing(sample[0])
    alive = parse_listing(sample[1])
    for lst in (gone, alive):
        storage.upsert_listing(lst)
        storage.record_price(lst)
        storage.mark_seen(watch.id, lst)

    _set_notified(gone.id, NOW - timedelta(days=40))
    _set_notified(alive.id, NOW - timedelta(days=1))

    result = storage.purge_old_history(retention_days=30, now=NOW)

    assert result["listings"] == 1
    seen = storage.seen_ids(watch.id)
    assert gone.id not in seen
    assert alive.id in seen
    # снимки цен ушедшего объявления тоже удалены -- сирот в price_snapshots не остаётся
    assert _prices(gone.id) == []


def test_purge_keeps_latest_price_snapshot_of_live_listing(db, sample):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    lst = parse_listing(sample[0])
    storage.upsert_listing(lst)
    storage.mark_seen(watch.id, lst)
    _set_notified(lst.id, NOW - timedelta(days=1))  # объявление ещё живо

    _add_snapshot(lst.id, 100, NOW - timedelta(days=40))  # старый, за пределами retention
    _add_snapshot(lst.id, 200, NOW - timedelta(days=1))  # свежий

    result = storage.purge_old_history(retention_days=30, now=NOW)

    assert result["listings"] == 0
    # свежий снимок остаётся -- по нему record_price ловит изменение цены
    assert _prices(lst.id) == [200]
    assert lst.id in storage.seen_ids(watch.id)


def test_purge_is_noop_when_everything_is_recent(db, sample):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)
    lst = parse_listing(sample[0])
    storage.upsert_listing(lst)
    storage.record_price(lst)
    storage.mark_seen(watch.id, lst)
    _set_notified(lst.id, NOW - timedelta(hours=2))

    assert storage.purge_old_history(retention_days=30, now=NOW) == {"listings": 0, "snapshots": 0}


async def test_maybe_purge_runs_at_most_once_per_day(db, monkeypatch):
    calls: list[datetime] = []

    def fake_purge(*, retention_days, now):
        calls.append(now)
        return {"listings": 0, "snapshots": 0}

    monkeypatch.setattr(monitor.storage, "purge_old_history", fake_purge)
    poller = Poller(proxies=[])

    assert await monitor.maybe_purge(poller, now=NOW) is True
    assert await monitor.maybe_purge(poller, now=NOW + timedelta(hours=5)) is False
    assert await monitor.maybe_purge(poller, now=NOW + timedelta(days=2)) is True
    assert len(calls) == 2
