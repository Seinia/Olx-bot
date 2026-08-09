from datetime import UTC, datetime

from olx.models import Filters, Listing, NotifyMode, OwnerType, PollMode, State, Watch


def _listing(**over):
    base = dict(
        id=928002010,
        url="https://www.olx.ua/d/uk/obyavlenie/x.html",
        title="Iphone 13 Pro Max 512 gb",
        description="",
        price=15100,
        currency="UAH",
        negotiable=False,
        arranged=False,
        city_id=639,
        city_name="Зарічне",
        region_id=14,
        business=False,
        state=State.USED,
        status="active",
        created_at=datetime(2026, 7, 4, 14, 55, 38, tzinfo=UTC),
        pushed_at=None,
        refreshed_at=None,
        photo_url=None,
        category_id=85,
        seller_id=795705545,
        seller_name="Сергій",
    )
    return Listing(**{**base, **over})


def test_listing_is_hashable_for_set_diff():
    # Детектор новизны сравнивает множества, а не порядок выдачи (S-07).
    assert len({_listing(), _listing()}) == 1


def test_is_active():
    assert _listing().is_active
    assert not _listing(status="removed_by_user").is_active


def test_price_may_be_absent():
    assert _listing(price=None, currency=None, arranged=True).price is None


def test_watch_defaults():
    w = Watch(id=1, query="iphone 13", filters=Filters())
    assert w.poll_mode is PollMode.FAST
    assert w.notify_mode is NotifyMode.NEW
    assert w.stop_words == []
    assert w.enabled


def test_watch_stop_words_not_shared_between_instances():
    a = Watch(id=1, query="a", filters=Filters())
    b = Watch(id=2, query="b", filters=Filters())
    a.stop_words.append("icloud")
    assert b.stop_words == []


def test_enum_values_match_api_contract():
    assert State.USED == "used"
    assert OwnerType.PRIVATE == "private"
    assert PollMode.FAST == "fast"
    assert NotifyMode.NEW_PUSHUP == "new_pushup"
