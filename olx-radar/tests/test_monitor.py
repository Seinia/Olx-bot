import random
from datetime import UTC, datetime, timedelta

from olx.models import Filters, Listing, NotifyMode, Watch
from olx.monitor import select_new_from

T0 = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


def _listing(listing_id: int, *, created=T0, pushed=None, refreshed=None) -> Listing:
    return Listing(
        id=listing_id,
        url=f"https://www.olx.ua/d/uk/obyavlenie/{listing_id}.html",
        title=f"Лот {listing_id}",
        description="",
        price=5000,
        currency="UAH",
        negotiable=False,
        arranged=False,
        city_id=268,
        city_name="Київ",
        region_id=25,
        business=False,
        state=None,
        status="active",
        created_at=created,
        pushed_at=pushed,
        refreshed_at=refreshed,
        photo_url=None,
        category_id=85,
        seller_id=1,
        seller_name="Продавець",
    )


def _watch(mode: NotifyMode) -> Watch:
    return Watch(id=1, query="iphone 13", filters=Filters(), notify_mode=mode)


def test_unseen_listing_is_new_in_both_modes():
    listings = [_listing(100)]
    for mode in (NotifyMode.NEW, NotifyMode.NEW_PUSHUP):
        finds = select_new_from(_watch(mode), listings, seen=set(), last_pushups={})
        assert [(f.listing.id, f.reason) for f in finds] == [(100, "new")]


def test_old_listing_surfacing_is_not_notified():
    # Требование владельца: старое объявление, впервые попавшее в выдачу, но
    # созданное ДО запроса, уведомлением не считается. Уведомляем только то, что
    # опубликовано после создания запроса.
    watch = Watch(
        id=1,
        query="iphone 13",
        filters=Filters(),
        notify_mode=NotifyMode.NEW,
        created_at=T0,
    )
    old = _listing(100, created=T0 - timedelta(days=130))
    fresh = _listing(200, created=T0 + timedelta(hours=1))

    finds = select_new_from(watch, [old, fresh], seen=set(), last_pushups={})
    assert [(f.listing.id, f.reason) for f in finds] == [(200, "new")]


def test_old_listing_pushed_after_watch_notifies_as_pushup():
    # Но если старое объявление ПОДНЯЛИ уже после создания запроса -- это событие
    # «с того момента, как создалась задача», его шлём (в режиме new_pushup).
    watch = Watch(
        id=1,
        query="iphone 13",
        filters=Filters(),
        notify_mode=NotifyMode.NEW_PUSHUP,
        created_at=T0,
    )
    old = _listing(100, created=T0 - timedelta(days=130), pushed=T0 + timedelta(hours=2))

    finds = select_new_from(watch, [old], seen={100}, last_pushups={100: T0})
    assert [(f.listing.id, f.reason) for f in finds] == [(100, "pushup")]


def test_seen_listing_is_silent_without_pushup():
    listings = [_listing(100, pushed=T0)]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP), listings, seen={100}, last_pushups={100: T0}
    )
    assert finds == []


def test_pushup_notifies_only_in_pushup_mode():
    """AC-12: переподнятое объявление уведомляет new_pushup и молчит для new."""
    listings = [_listing(100, created=T0, pushed=T0 + timedelta(hours=1))]
    seen = {100}
    old = {100: T0}

    silent = select_new_from(_watch(NotifyMode.NEW), listings, seen=seen, last_pushups=old)
    assert silent == []

    loud = select_new_from(_watch(NotifyMode.NEW_PUSHUP), listings, seen=seen, last_pushups=old)
    assert [(f.listing.id, f.reason) for f in loud] == [(100, "pushup")]


def test_pushup_without_recorded_baseline_stays_silent_and_seeds_it():
    # База отсутствует (None) -- НЕ уведомляем, просто запоминаем как отправную
    # точку. Иначе любое новое отслеживаемое поле, добавленное к уже работающему
    # боту, при первом опросе разом пометило бы «поднятым» весь бэклог (см.
    # test_refresh_without_recorded_baseline_stays_silent ниже -- ровно этот
    # сценарий произошёл на проде с last_refresh_at).
    listings = [_listing(100, pushed=T0)]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP), listings, seen={100}, last_pushups={100: None}
    )
    assert finds == []


def test_older_pushup_does_not_notify():
    # Выдача может прийти с узла с устаревшими данными -- откат времени подъёма
    # не должен считаться новым событием.
    listings = [_listing(100, pushed=T0 - timedelta(hours=1))]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP), listings, seen={100}, last_pushups={100: T0}
    )
    assert finds == []


def test_result_is_independent_of_response_order():
    """S-07: порядок выдачи OLX нестабилен, результат не должен от него зависеть."""
    listings = [
        _listing(101, created=T0),
        _listing(102, created=T0 + timedelta(minutes=1)),
        _listing(103, created=T0 + timedelta(minutes=2)),
    ]
    watch = _watch(NotifyMode.NEW)
    expected = [f.listing.id for f in select_new_from(watch, listings, seen=set(), last_pushups={})]

    rng = random.Random(0)
    for _ in range(20):
        shuffled = listings[:]
        rng.shuffle(shuffled)
        got = [f.listing.id for f in select_new_from(watch, shuffled, seen=set(), last_pushups={})]
        assert got == expected


def test_mixed_new_and_pushup_in_one_batch():
    listings = [
        _listing(200, created=T0 + timedelta(minutes=5)),
        _listing(100, created=T0, pushed=T0 + timedelta(hours=1)),
    ]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP), listings, seen={100}, last_pushups={100: T0}
    )
    assert [(f.listing.id, f.reason) for f in finds] == [(100, "pushup"), (200, "new")]


# --- last_refresh_time: платное продвижение (автоподъём) двигает ЭТО поле, не
# pushup_time -- подтверждено сверкой с живым ответом API OLX (2026-08-09, объявление
# ID10PorN с promotion.options=["bundle_basic"]): last_refresh_time совпало день-в-день
# с "Опубліковано" на странице, а pushup_time стоял на месте 11 дней с последнего
# ручного подъёма. Без учёта этого поля бот такие автоподъёмы не видел вовсе.


def test_refresh_without_pushup_still_notifies_as_pushup():
    # Ключевой сценарий из живого случая: pushup_time не изменился вообще
    # (pushed=None здесь эквивалентно "не сдвинулся"), а refreshed_at вырос.
    listings = [_listing(100, refreshed=T0 + timedelta(hours=1))]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP),
        listings,
        seen={100},
        last_pushups={100: None},
        last_refreshes={100: T0},
    )
    assert [f.reason for f in finds] == ["pushup"]


def test_refresh_without_recorded_baseline_stays_silent_and_seeds_it():
    # Прямое воспроизведение прод-инцидента: миграция добавила last_refresh_at,
    # у уже отслеживаемых объявлений база была None -- и первый же опрос после
    # обновления пометил чуть ли не весь бэклог как "поднято". Правильно --
    # тихо запомнить значение сейчас, а уведомлять только о РОСТЕ относительно него.
    listings = [_listing(100, refreshed=T0)]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP),
        listings,
        seen={100},
        last_pushups={100: None},
        last_refreshes={100: None},
    )
    assert finds == []


def test_older_refresh_does_not_notify():
    listings = [_listing(100, refreshed=T0 - timedelta(hours=1))]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP),
        listings,
        seen={100},
        last_pushups={100: None},
        last_refreshes={100: T0},
    )
    assert finds == []


def test_pushup_and_refresh_both_present_notify_only_once():
    # Оба поля сдвинулись разом -- одно событие "pushup", не два.
    listings = [_listing(100, pushed=T0 + timedelta(hours=1), refreshed=T0 + timedelta(hours=1))]
    finds = select_new_from(
        _watch(NotifyMode.NEW_PUSHUP),
        listings,
        seen={100},
        last_pushups={100: T0},
        last_refreshes={100: T0},
    )
    assert len(finds) == 1
    assert finds[0].reason == "pushup"


def test_refresh_mode_respects_new_only_setting():
    listings = [_listing(100, refreshed=T0 + timedelta(hours=1))]
    finds = select_new_from(
        _watch(NotifyMode.NEW),
        listings,
        seen={100},
        last_pushups={100: None},
        last_refreshes={100: T0},
    )
    assert finds == []
