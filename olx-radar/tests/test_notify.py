import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from olx import notify
from olx.config import settings
from olx.models import Filters, Listing, NotifyMode, PollMode, Watch

FIXED_NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

# Ссылка собрана по реальному шаблону CDN (контракты, п.1.2), но id файла
# заведомо несуществующий -- Telegram не сможет её скачать. Это не гипотетический
# случай: OLX может сменить формат шаблона, и sendPhoto обязан деградировать,
# а не терять уведомление.
BROKEN_PHOTO_URL = "https://ireland.apollo.olxcdn.com:443/v1/files/does-not-exist-test-id/image;s=800x600"
GOOD_PHOTO_URL = "https://telegram.org/img/t_logo.png"


def _listing(**overrides) -> Listing:
    base = dict(
        id=900000001,
        url="https://www.olx.ua/d/obyavlenie/test-ID900000001.html",
        title="[ТЕСТ T-10] Айфон 13 Про, чохол в подарок — не бита",
        description="",
        price=15100,
        currency="UAH",
        negotiable=False,
        arranged=False,
        city_id=268,
        city_name="Київ",
        region_id=1,
        business=False,
        state=None,
        status="active",
        created_at=datetime.now(UTC),
        pushed_at=None,
        refreshed_at=None,
        photo_url=GOOD_PHOTO_URL,
        category_id=85,
        seller_id=1,
        seller_name="Тест",
    )
    base.update(overrides)
    return Listing(**base)


def _watch() -> Watch:
    return Watch(
        id=1,
        query="iphone 13",
        filters=Filters(),
        poll_mode=PollMode.FAST,
        notify_mode=NotifyMode.NEW,
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # _next_allowed_at общий на модуль -- без сброса тест из одного файла мог бы
    # унаследовать дедлайн ожидания от предыдущего и зависнуть на реальном sleep.
    notify._next_allowed_at = 0.0
    yield
    notify._next_allowed_at = 0.0


async def test_throttle_lets_the_first_call_through_immediately(monkeypatch):
    monkeypatch.setattr(notify.time, "monotonic", lambda: 1000.0)
    slept = []
    monkeypatch.setattr(notify.asyncio, "sleep", _record_sleep(slept))

    await notify._throttle()

    assert slept == []


async def test_throttle_waits_out_the_minimum_interval(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock["t"])
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)

    await notify._throttle()
    await notify._throttle()

    # Между вызовами должно набежать не меньше MIN_REQUEST_INTERVAL_SECONDS --
    # ровно то, что защищает от 429 при пачке фото одного объявления.
    assert slept == [pytest.approx(notify.MIN_REQUEST_INTERVAL_SECONDS)]


async def test_postpone_pushes_the_next_allowed_call_into_the_future(monkeypatch):
    monkeypatch.setattr(notify.time, "monotonic", lambda: 1000.0)

    await notify._postpone(537.0)

    assert notify._next_allowed_at == pytest.approx(1537.0)


async def test_postpone_never_moves_the_deadline_earlier(monkeypatch):
    monkeypatch.setattr(notify.time, "monotonic", lambda: 1000.0)
    await notify._postpone(500.0)

    monkeypatch.setattr(notify.time, "monotonic", lambda: 1400.0)
    await notify._postpone(10.0)  # 1400+10=1410, меньше уже выставленных 1500

    assert notify._next_allowed_at == pytest.approx(1500.0)


def test_retry_after_prefers_the_header_over_the_body():
    response = httpx.Response(
        429,
        headers={"Retry-After": "537"},
        json={"parameters": {"retry_after": 5}},
    )
    assert notify._retry_after_seconds(response) == 537.0


def test_retry_after_falls_back_to_the_body_when_header_is_missing():
    response = httpx.Response(429, json={"parameters": {"retry_after": 42}})
    assert notify._retry_after_seconds(response) == 42.0


def test_retry_after_defaults_to_a_safe_pause_when_nothing_is_parseable():
    response = httpx.Response(429, content=b"not json")
    assert notify._retry_after_seconds(response) == 5.0


def _record_sleep(bucket: list):
    async def _fake(seconds):
        bucket.append(seconds)

    return _fake


@pytest.mark.telegram
async def test_send_listing_delivers_via_send_photo():
    await asyncio.sleep(1)
    listing = _listing(title="[ТЕСТ T-10] карточка через sendPhoto, не реальная находка")
    await notify.send_listing(listing, _watch(), reason="new")


@pytest.mark.telegram
async def test_send_photo_rejects_broken_url_and_send_listing_falls_back():
    await asyncio.sleep(1)

    # Проверяем сам факт отказа Telegram отдельно от send_listing, иначе тест
    # "не упал" ничего не доказывает -- он прошёл бы и если бы sendPhoto молча
    # съел битую ссылку.
    async with httpx.AsyncClient(timeout=30.0) as client:
        ok = await notify._send_photo(
            client,
            settings.telegram_owner_id,
            BROKEN_PHOTO_URL,
            "[ТЕСТ T-10] проверка отказа sendPhoto",
        )
    assert ok is False

    listing = _listing(
        id=900000002,
        title="[ТЕСТ T-10] карточка с битым фото — должна дойти текстом",
        photo_url=BROKEN_PHOTO_URL,
    )
    await notify.send_listing(listing, _watch(), reason="pushup")


@pytest.mark.telegram
async def test_send_listing_without_photo_uses_send_message():
    await asyncio.sleep(1)
    listing = _listing(
        id=900000003,
        title="[ТЕСТ T-10] карточка без фото вообще",
        photo_url=None,
        price=None,
        arranged=True,
    )
    await notify.send_listing(listing, _watch(), reason="new")


@pytest.mark.telegram
async def test_send_alert_delivers():
    await asyncio.sleep(1)
    await notify.send_alert("[ТЕСТ T-10] send_alert — проверка канала, реальных находок нет")


def test_caption_keeps_cyrillic_and_marks_pushup():
    listing = _listing(price=None)
    caption = notify._caption(listing, _watch(), reason="pushup")
    assert "Айфон" in caption
    assert "Переподнято" in caption
    assert "Договорная" in caption


def test_caption_marks_new():
    listing = _listing()
    caption = notify._caption(listing, _watch(), reason="new")
    assert "Новое" in caption
    assert "15 100 UAH" in caption


def test_caption_escapes_html_in_title():
    listing = _listing(title="<script>alert(1)</script> & тест")
    caption = notify._caption(listing, _watch(), reason="new")
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption


def test_caption_converts_olx_html_description_to_plain_text():
    listing = _listing(description="Перший рядок<br />\n<br />\nДругий &amp; <b>важливий</b> рядок")

    caption = notify._caption(listing, _watch(), reason="new")

    assert "Перший рядок\n\nДругий &amp; важливий рядок" in caption
    assert "&lt;br" not in caption


def test_age_humanizes():
    assert notify._age(FIXED_NOW - timedelta(seconds=10), now=FIXED_NOW) == "только что"
    assert notify._age(FIXED_NOW - timedelta(minutes=5), now=FIXED_NOW) == "5 мин назад"
    assert notify._age(FIXED_NOW - timedelta(hours=3), now=FIXED_NOW) == "3 ч назад"
    assert notify._age(FIXED_NOW - timedelta(days=5), now=FIXED_NOW) == "5 дн назад"
    assert notify._age(FIXED_NOW - timedelta(days=200), now=FIXED_NOW) == "6 мес назад"


def test_when_lines_new_is_single_recent_line():
    lst = _listing(created_at=FIXED_NOW - timedelta(hours=2))
    lines = notify._when_lines(lst, "new", now=FIXED_NOW)
    assert len(lines) == 1
    assert "2 ч назад" in lines[0]
    # у нового префикс "опубликовано" не нужен -- дата и так свежая
    assert "опубликовано" not in lines[0]


def test_when_lines_new_but_late_explains_the_gap():
    # Ровно случай владельца: опубликовано 2 дня назад, замечено только сейчас.
    lst = _listing(created_at=FIXED_NOW - timedelta(days=2))
    lines = notify._when_lines(lst, "new", now=FIXED_NOW)
    assert any("опубликовано" in ln for ln in lines)
    assert any("замечено спустя 2 дн" in ln and "изменилась цена" in ln for ln in lines)


def test_when_lines_pushup_shows_pushup_moment():
    lst = _listing(
        created_at=FIXED_NOW - timedelta(days=130),
        pushed_at=FIXED_NOW - timedelta(hours=1),
    )
    lines = notify._when_lines(lst, "pushup", now=FIXED_NOW)
    assert any("опубликовано" in ln for ln in lines)
    assert any("поднято" in ln and "1 ч назад" in ln for ln in lines)


def test_caption_pushup_marks_and_explains_old_date():
    lst = _listing(
        created_at=datetime.now(UTC) - timedelta(days=130),
        pushed_at=datetime.now(UTC) - timedelta(hours=3),
    )
    caption = notify._caption(lst, _watch(), reason="pushup")
    assert "Переподнято" in caption
    assert "опубликовано" in caption
    assert "поднято" in caption


def _capture(monkeypatch):
    seq: list = []

    async def fake_group(client, chat_id, urls, caption):
        seq.append(("group", tuple(urls), caption is not None))
        return True

    async def fake_photo(client, chat_id, url, caption=None):
        seq.append(("photo", url, caption is not None))
        return True

    async def fake_msg(client, chat_id, text):
        seq.append(("msg", text))

    monkeypatch.setattr(notify, "_send_media_group", fake_group)
    monkeypatch.setattr(notify, "_send_photo", fake_photo)
    monkeypatch.setattr(notify, "_send_message", fake_msg)
    return seq


async def test_multiple_photos_go_as_one_media_group(monkeypatch):
    seq = _capture(monkeypatch)
    lst = _listing(photo_urls=("u1", "u2", "u3"), photo_url="u1")
    await notify.send_listing(lst, _watch(), reason="new")
    assert [c[0] for c in seq] == ["group"]
    assert seq[0][1] == ("u1", "u2", "u3")
    assert seq[0][2] is True


async def test_media_group_splits_over_ten_photos(monkeypatch):
    seq = _capture(monkeypatch)
    urls = tuple(f"u{i}" for i in range(13))
    lst = _listing(photo_urls=urls, photo_url=urls[0])
    await notify.send_listing(lst, _watch(), reason="new")
    groups = [c for c in seq if c[0] == "group"]
    assert [len(g[1]) for g in groups] == [10, 3]
    # подпись только на первом альбоме, иначе Telegram покажет её дважды
    assert groups[0][2] is True
    assert groups[1][2] is False


async def test_media_group_falls_back_to_individual_photos(monkeypatch):
    seq: list = []

    async def failing_group(client, chat_id, urls, caption):
        seq.append("group")
        return False

    async def fake_photo(client, chat_id, url, caption=None):
        seq.append(("photo", caption is not None))
        return True

    async def fake_msg(client, chat_id, text):
        seq.append("msg")

    monkeypatch.setattr(notify, "_send_media_group", failing_group)
    monkeypatch.setattr(notify, "_send_photo", fake_photo)
    monkeypatch.setattr(notify, "_send_message", fake_msg)

    lst = _listing(photo_urls=("u1", "u2"), photo_url="u1")
    await notify.send_listing(lst, _watch(), reason="new")

    assert "group" in seq
    assert ("photo", True) in seq  # подпись доехала на первом фото
    assert "msg" not in seq  # до текстового фоллбэка дойти не должно
