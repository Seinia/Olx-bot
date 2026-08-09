from datetime import UTC, datetime, timedelta

from olx import texts
from olx.models import Filters, NotifyMode, OwnerType, PollMode, State, Watch

NOW = datetime.now(UTC)


def _watch(**over):
    base = dict(
        id=3,
        query="Квартира",
        filters=Filters(
            price_from=9000, price_to=15000, city_id=268, category_id=1760,
            state=State.USED, owner_type=OwnerType.PRIVATE,
        ),
        stop_words=["подобово", "студія"],
        poll_mode=PollMode.FAST,
        notify_mode=NotifyMode.NEW_PUSHUP,
        enabled=True,
        created_at=NOW - timedelta(days=1),
        last_polled_at=NOW - timedelta(seconds=30),
        last_found_at=NOW - timedelta(minutes=42),
    )
    return Watch(**{**base, **over})


def test_card_shows_names_not_identifiers():
    card = texts.watch_card(_watch())
    assert "Київ" in card
    assert "оренда квартир" in card
    assert "268" not in card
    assert "1760" not in card


def test_card_separates_what_from_how_with_a_blank_line():
    # Пустая строка между фильтрами и режимами — единственное, что не даёт карточке
    # слипнуться в сплошной список.
    assert "\n\n" in texts.watch_card(_watch())


def test_card_omits_absent_filters():
    card = texts.watch_card(_watch(filters=Filters(), stop_words=[]))
    assert "📍" not in card
    assert "🚫" not in card
    assert "любая" in card


def test_card_shows_region_when_no_specific_city_is_set():
    card = texts.watch_card(_watch(filters=Filters(category_id=108, region_id=21)))
    assert "🗺" in card
    assert "📍" not in card


def test_watch_label_falls_back_to_category_when_query_is_empty():
    # Режим «весь раздел без фразы» -- пустая query не должна попасть в кнопку
    # как есть: пустая подпись выглядит как сломанная кнопка.
    watch = _watch(query="", filters=Filters(category_id=1760))
    label = texts.watch_label(watch)
    assert label
    assert "оренда квартир" in label


def test_watch_label_uses_query_when_present():
    assert texts.watch_label(_watch(query="Квартира")) == "Квартира"


def test_price_formats():
    assert "9 000 – 15 000 грн" in texts.watch_card(_watch())
    assert "до 8 000 грн" in texts.watch_card(_watch(filters=Filters(price_to=8000)))
    assert "от 3 000 грн" in texts.watch_card(_watch(filters=Filters(price_from=3000)))


def test_paused_watch_is_marked():
    assert "на паузе" in texts.watch_card(_watch(enabled=False))
    assert "активен" in texts.watch_card(_watch(enabled=True))


def test_relative_time_is_human():
    assert "только что" in texts.watch_card(_watch(last_found_at=NOW))
    assert "42 мин назад" in texts.watch_card(_watch())
    assert "3 ч назад" in texts.watch_card(_watch(last_found_at=NOW - timedelta(hours=3)))
    assert "ещё не было" in texts.watch_card(_watch(last_found_at=None))


def test_query_is_escaped():
    assert "&lt;b&gt;" in texts.watch_card(_watch(query="<b>дом</b> & сад"))


def test_list_is_empty_hint_when_nothing_tracked():
    assert "/add" in texts.watch_list([])


def test_list_separates_cards():
    out = texts.watch_list([_watch(), _watch(id=4)])
    assert texts.RULE in out


def test_status_warns_when_everything_paused():
    out = texts.status([_watch(enabled=False)], fast_interval=25)
    assert "приостановлен" in out
    assert "/resume" in out


def test_status_mentions_interval_when_running():
    assert "25" in texts.status([_watch()], fast_interval=25)


def test_help_lists_every_command():
    for command in ("/add", "/list", "/edit", "/del", "/status", "/pause", "/resume", "/export"):
        assert command in texts.COMMANDS


def test_step_header_shows_progress():
    assert "Шаг 2 из 7" in texts.step(2, 7, "Раздел", "тело")
