import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olx import monitor, storage
from olx.config import settings
from olx.errors import BlockedError, SchemaError, TransportError
from olx.models import Filters, NotifyMode, PollMode
from olx.monitor import Poller

# Тестовый Telegram user_id -- storage.add_watch() теперь мультипользовательский
# и требует владельца; какой именно id, для большинства тестов не важно.
USER = 999999

SAMPLE = (
    Path(__file__).resolve().parent / "fixtures" / "sample-response.json"
)


@pytest.fixture
def db(tmp_path):
    storage.init_db(tmp_path / "monitor.db")


@pytest.fixture
def watch(db):
    return storage.add_watch("iphone 13", Filters(), PollMode.FAST, NotifyMode.NEW, user_id=USER)


@pytest.fixture
def sample_response():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def sent(monkeypatch):
    box = {"alerts": [], "listings": []}

    async def _alert(text):
        box["alerts"].append(text)

    async def _listing(listing, w, *, reason, old_price=None):
        box["listings"].append((listing.id, reason))

    monkeypatch.setattr(monitor.notify, "send_alert", _alert)
    monkeypatch.setattr(monitor.notify, "send_listing", _listing)
    return box


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(monitor.asyncio, "sleep", _sleep)
    return slept


def test_backoff_grows_and_is_capped():
    p = Poller(proxies=[])
    delays = [p.on_blocked() for _ in range(12)]
    assert delays == sorted(delays)
    assert delays[-1] <= settings.backoff_max


def test_proxy_rotates_on_block(monkeypatch):
    monkeypatch.setattr(settings, "proxy_enabled", True)
    p = Poller(proxies=["p1", "p2", "p3"])
    seen = [p.proxy]
    for _ in range(3):
        p.on_blocked()
        seen.append(p.proxy)
    assert seen == ["p1", "p2", "p3", "p1"]


def test_success_resets_failure_counter():
    p = Poller(proxies=[])
    p.on_blocked()
    p.on_blocked()
    assert p.failures == 2
    p.on_success()
    assert p.failures == 0


@pytest.mark.asyncio
async def test_ac8_survives_three_blocks_and_recovers(
    watch, sent, no_sleep, sample_response, monkeypatch
):
    """AC-8: три 403 подряд -> backoff, алерт, процесс жив, восстановление само."""
    calls = {"n": 0}

    def _search(query, filters, *, offset=0, limit=40, proxy=None):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise BlockedError("403 (заглушка)")
        return sample_response

    monkeypatch.setattr(monitor.api, "search_raw", _search)
    poller = Poller(proxies=["p1", "p2"])

    for _ in range(3):
        await monitor._cycle(poller)

    assert poller.failures == 3
    assert no_sleep == sorted(no_sleep)
    # Алерт один, а не три: при затяжной блокировке поток сообщений сам становится спамом.
    assert len(sent["alerts"]) == 1
    assert "блокирует" in sent["alerts"][0].lower()

    await monitor._cycle(poller)

    # Восстановление доказывается не уведомлениями (первый удачный опрос -- посевной
    # и молчит по замыслу), а тем, что счётчик сброшен и выдача реально осела в БД.
    assert poller.failures == 0
    assert len(storage.seen_ids(watch.id)) == 52


@pytest.mark.asyncio
async def test_schema_error_pauses_watch_and_alerts(watch, sent, no_sleep, monkeypatch):
    def _search(*a, **kw):
        raise SchemaError("нет ключа data")

    monkeypatch.setattr(monitor.api, "search_raw", _search)
    await monitor._cycle(Poller(proxies=[]))

    assert [w.enabled for w in storage.list_watches(enabled_only=False)] == [False]
    assert "формат" in sent["alerts"][0]


@pytest.mark.asyncio
async def test_transport_error_does_not_pause_watch(watch, sent, no_sleep, monkeypatch):
    # Обрыв сети -- это не поломка парсера: запрос должен остаться активным
    # и попробовать снова на следующем цикле.
    def _search(*a, **kw):
        raise TransportError("таймаут")

    monkeypatch.setattr(monitor.api, "search_raw", _search)
    await monitor._cycle(Poller(proxies=[]))

    assert [w.enabled for w in storage.list_watches(enabled_only=False)] == [True]
    assert sent["alerts"] == []


@pytest.mark.asyncio
async def test_first_poll_seeds_silently(watch, sent, no_sleep, sample_response, monkeypatch):
    """Создание запроса не должно выливаться в пачку уведомлений о том, что уже висит."""
    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: sample_response)

    await monitor._cycle(Poller(proxies=[]))

    assert sent["listings"] == []
    assert len(storage.seen_ids(watch.id)) == 52


def _make_due(watch_id: int) -> None:
    """Сдвигает последний опрос в прошлое, чтобы запрос снова стал «пора».

    После T-21 циклы подряд не опрашивают один и тот же запрос: у каждого свой
    интервал. Тестам, гоняющим несколько циклов вплотную, нужно двигать время.
    """
    storage.update_watch(watch_id, last_polled_at=datetime.now(UTC) - timedelta(hours=1))


@pytest.mark.asyncio
async def test_only_listings_added_after_seeding_are_notified(
    watch, sent, no_sleep, sample_response, monkeypatch
):
    responses = [sample_response]
    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: responses[-1])
    poller = Poller(proxies=[])

    await monitor._cycle(poller)
    assert sent["listings"] == []

    fresh = json.loads(json.dumps(sample_response["data"][0]))
    fresh["id"] = 999_000_001
    # Опубликовано после создания запроса -- значит, по-настоящему новое.
    fresh["created_time"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    responses.append({**sample_response, "data": [fresh, *sample_response["data"]]})

    _make_due(watch.id)
    await monitor._cycle(poller)
    assert [lid for lid, _ in sent["listings"]] == [999_000_001]

    _make_due(watch.id)
    await monitor._cycle(poller)
    assert [lid for lid, _ in sent["listings"]] == [999_000_001]


def test_asyncio_is_reachable_for_patching():
    assert asyncio is monitor.asyncio


@pytest.mark.asyncio
async def test_ac9_silence_alert_fires_when_threshold_is_zero(watch, sent, monkeypatch):
    """AC-9: при SILENCE_ALERT_HOURS=0 алерт приходит в первом же цикле."""
    monkeypatch.setattr(settings, "silence_alert_hours", 0)
    assert await monitor.check_silence(Poller(proxies=[])) is True
    assert "находок нет" in sent["alerts"][0].lower()


@pytest.mark.asyncio
async def test_silence_alert_is_not_repeated_every_cycle(watch, sent, monkeypatch):
    monkeypatch.setattr(settings, "silence_alert_hours", 0)
    poller = Poller(proxies=[])
    for _ in range(5):
        await monitor.check_silence(poller)
    assert len(sent["alerts"]) == 1


@pytest.mark.asyncio
async def test_no_silence_alert_within_threshold(watch, sent, monkeypatch):
    monkeypatch.setattr(settings, "silence_alert_hours", 12)
    assert await monitor.check_silence(Poller(proxies=[])) is False
    assert sent["alerts"] == []


@pytest.mark.asyncio
async def test_fresh_watch_does_not_trigger_silence_immediately(watch, sent, monkeypatch):
    # Запрос создан только что и ещё ничего не находил -- это не тишина,
    # это отсутствие истории. Отсчёт идёт от created_at.
    monkeypatch.setattr(settings, "silence_alert_hours", 1)
    assert await monitor.check_silence(Poller(proxies=[])) is False


@pytest.mark.asyncio
async def test_silence_flag_resets_after_a_find(
    watch, sent, no_sleep, sample_response, monkeypatch
):
    monkeypatch.setattr(settings, "silence_alert_hours", 0)
    poller = Poller(proxies=[])
    await monitor.check_silence(poller)
    assert poller.silence_alerted

    monkeypatch.setattr(monitor.api, "search_raw", lambda *a, **kw: sample_response)
    await monitor._cycle(poller)
    monkeypatch.setattr(settings, "silence_alert_hours", 12)
    await monitor.check_silence(poller)
    assert not poller.silence_alerted
