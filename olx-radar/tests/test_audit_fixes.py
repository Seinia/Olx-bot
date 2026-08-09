"""Регрессии на дефекты, найденные аудитом стадии 8.

Каждый тест воспроизводит конкретную находку. Общее у них одно: все семь дефектов
жили на стыке между модулями, а не внутри них, и ни один не ловился тестами
отдельных функций.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olx import monitor, storage
from olx.config import settings
from olx.errors import SchemaError, StorageError
from olx.models import Filters, NotifyMode, PollMode
from olx.monitor import Poller

SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "sample-response.json"
)


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    storage.init_db(tmp_path / "fixes.db")


@pytest.fixture
def sent(monkeypatch):
    box = {"alerts": [], "listings": []}

    async def _alert(text):
        box["alerts"].append(text)

    async def _listing(listing, w, *, reason):
        box["listings"].append((listing.id, reason))

    monkeypatch.setattr(monitor.notify, "send_alert", _alert)
    monkeypatch.setattr(monitor.notify, "send_listing", _listing)
    return box


@pytest.fixture
def no_sleep(monkeypatch):
    async def _sleep(seconds):
        return None

    monkeypatch.setattr(monitor.asyncio, "sleep", _sleep)


def _due(watch_id):
    storage.update_watch(watch_id, last_polled_at=datetime.now(UTC) - timedelta(hours=2))


# --- T-21: расписание по режимам ---------------------------------------------


def test_full_mode_is_not_polled_at_fast_interval(db):
    watch = storage.add_watch("q", Filters(), PollMode.FULL, NotifyMode.NEW)
    storage.update_watch(watch.id, last_polled_at=datetime.now(UTC) - timedelta(seconds=60))
    watch = storage.list_watches()[0]

    # 60 с прошло: для быстрого режима давно пора, для полного (600 с) — нет.
    assert monitor.interval_for(watch) == settings.poll_interval_full
    assert not monitor.is_due(watch, datetime.now(UTC))


def test_fast_mode_is_due_after_its_interval(db):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    storage.update_watch(
        watch.id, last_polled_at=datetime.now(UTC) - timedelta(seconds=settings.poll_interval_fast)
    )
    assert monitor.is_due(storage.list_watches()[0], datetime.now(UTC))


def test_sleep_waits_for_the_nearest_due_watch(db):
    fast = storage.add_watch("f", Filters(), PollMode.FAST, NotifyMode.NEW)
    slow = storage.add_watch("s", Filters(), PollMode.FULL, NotifyMode.NEW)
    now = datetime.now(UTC)
    storage.update_watch(fast.id, last_polled_at=now - timedelta(seconds=10))
    storage.update_watch(slow.id, last_polled_at=now)

    wait = monitor.seconds_until_next(storage.list_watches(), now)
    assert wait <= settings.poll_interval_fast - 10 + 1


# --- T-22: уведомления не зацикливаются и не теряются -------------------------


def test_pushup_does_not_loop_when_nodes_disagree(db, sample):
    """Аудит: чередование узлов с разным pushup_time уведомляло бесконечно."""
    from olx.parse import parse_listing

    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW_PUSHUP)
    raw = sample["data"][0]

    def at(hour):
        item = json.loads(json.dumps(raw))
        item["pushup_time"] = f"2026-07-22T{hour:02d}:00:00+03:00"
        return parse_listing(item)

    node_a, node_b = at(12), at(11)

    storage.upsert_listing(node_a)
    storage.mark_seen(watch.id, node_a)
    baseline = storage.last_pushup(watch.id, node_a.id)

    # Ответ отставшего узла не должен откатывать отметку.
    storage.mark_seen(watch.id, node_b)
    assert storage.last_pushup(watch.id, node_a.id) == baseline


def test_pushup_survives_null_from_a_node(db, sample):
    from olx.parse import parse_listing

    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW_PUSHUP)
    raw = json.loads(json.dumps(sample["data"][0]))
    raw["pushup_time"] = "2026-07-22T12:00:00+03:00"
    with_time = parse_listing(raw)

    raw_null = json.loads(json.dumps(sample["data"][0]))
    raw_null["pushup_time"] = None
    without_time = parse_listing(raw_null)

    storage.upsert_listing(with_time)
    storage.mark_seen(watch.id, with_time)
    before = storage.last_pushup(watch.id, with_time.id)

    storage.mark_seen(watch.id, without_time)
    assert storage.last_pushup(watch.id, with_time.id) == before


@pytest.mark.asyncio
async def test_failed_send_does_not_kill_cycle_and_keeps_listing_unseen(
    db, sample, sent, no_sleep, monkeypatch
):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: sample)

    await monitor._cycle(Poller(proxies=[]))  # посев
    seeded = storage.seen_ids(watch.id)

    fresh = json.loads(json.dumps(sample["data"][0]))
    fresh["id"] = 777_000_001
    # Опубликовано после создания запроса -- иначе это не «новое», а старое всплывшее,
    # которое мы намеренно не шлём.
    fresh["created_time"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(
        monitor.api, "search_raw", lambda *a, **kw: {**sample, "data": [fresh, *sample["data"]]}
    )

    async def _boom(*a, **kw):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(monitor.notify, "send_listing", _boom)
    _due(watch.id)

    await monitor._cycle(Poller(proxies=[]))  # не должен упасть

    # Объявление не помечено увиденным -- уйдёт на следующем опросе, а не пропадёт.
    assert 777_000_001 not in storage.seen_ids(watch.id)
    assert len(storage.seen_ids(watch.id)) == len(seeded)


@pytest.mark.asyncio
async def test_alert_failure_does_not_kill_cycle(db, sample, no_sleep, monkeypatch):
    storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)

    def _blocked(*a, **kw):
        from olx.errors import BlockedError

        raise BlockedError("403")

    async def _boom(*a, **kw):
        raise ConnectionError("сеть отвалилась вместе с блокировкой")

    monkeypatch.setattr(monitor.api, "search_raw", _blocked)
    monkeypatch.setattr(monitor.notify, "send_alert", _boom)

    await monitor._cycle(Poller(proxies=[]))  # не должен упасть


# --- T-23: поломка формата падает громко --------------------------------------


@pytest.mark.parametrize(
    "broken",
    [{"results": []}, {"data": None}, {"data": "не список"}, {}],
)
def test_broken_response_raises_instead_of_looking_empty(broken):
    with pytest.raises(SchemaError):
        monitor._data_of(broken)


@pytest.mark.asyncio
async def test_broken_format_pauses_watch_and_alerts(db, sent, no_sleep, monkeypatch):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: {"results": []})

    await monitor._cycle(Poller(proxies=[]))

    assert not storage.list_watches(enabled_only=False)[0].enabled
    assert any("формат" in a for a in sent["alerts"])
    assert watch.id


# --- T-24: посев не сгорает на пустой выдаче ----------------------------------


@pytest.mark.asyncio
async def test_empty_first_poll_does_not_consume_seeding(db, sample, sent, no_sleep, monkeypatch):
    watch = storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)
    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: {"data": []})

    await monitor._cycle(Poller(proxies=[]))
    assert storage.list_watches()[0].last_polled_at is None, "признак посева должен остаться"

    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: sample)
    await monitor._cycle(Poller(proxies=[]))

    # Второй опрос — всё ещё посевной, а не пачка из 52 уведомлений.
    assert sent["listings"] == []
    assert len(storage.seen_ids(watch.id)) == 52


# --- T-25: опрос не блокирует event loop --------------------------------------


@pytest.mark.asyncio
async def test_polling_does_not_freeze_the_event_loop(db, sample, sent, monkeypatch):
    storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)

    import time as _time

    def _slow(*a, **kw):
        _time.sleep(0.6)
        return sample

    monkeypatch.setattr(monitor.api, "search_raw", _slow)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await monitor._cycle(Poller(proxies=[]))
    beat.cancel()

    # Раньше здесь было ровно 0: синхронный опрос держал loop целиком.
    assert ticks > 3, f"event loop замирал во время опроса, тиков: {ticks}"


# --- T-26: устойчивость хранилища ---------------------------------------------


@pytest.mark.asyncio
async def test_storage_error_does_not_kill_cycle(db, sample, sent, no_sleep, monkeypatch):
    storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)

    def _locked(*a, **kw):
        raise StorageError("database is locked")

    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: sample)
    monkeypatch.setattr(monitor.storage, "upsert_listing", _locked)

    await monitor._cycle(Poller(proxies=[]))  # не должен упасть
    assert any("База недоступна" in a for a in sent["alerts"])


@pytest.mark.asyncio
async def test_repeated_network_failures_raise_an_alert(db, sent, no_sleep, monkeypatch):
    storage.add_watch("q", Filters(), PollMode.FAST, NotifyMode.NEW)

    def _down(*a, **kw):
        from olx.errors import TransportError

        raise TransportError("таймаут")

    monkeypatch.setattr(monitor.api, "search_raw", _down)
    poller = Poller(proxies=[])

    for _ in range(monitor.TRANSPORT_ALERT_AFTER):
        await monitor._cycle(poller)
        storage.update_watch(1, last_polled_at=None)

    assert any("Сеть недоступна" in a for a in sent["alerts"])
