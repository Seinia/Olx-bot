"""Тексты и раскладка сообщений бота.

Отделено от обработчиков: в bot.py остаётся логика диалога, здесь — то, как это
выглядит у владельца в чате. Правка формулировки не должна заставлять лезть в FSM.

Telegram понимает из HTML только b, i, u, s, code, pre, a и blockquote. Ни списков,
ни таблиц, ни переносов по ширине — вся структура держится на пустых строках и эмодзи.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from olx import categories, geo
from olx.models import NotifyMode, PollMode, Watch

RULE = "─" * 18

START = """<b>OLX-монитор</b>

Слежу за объявлениями на olx.ua и присылаю новые, как только они появляются.

{commands}"""

COMMANDS = """<b>Запросы</b>
/add — добавить новый
/list — показать все
/edit — поменять цену, стоп-слова, режимы
/del — удалить

<b>Управление</b>
/status — что сейчас происходит
/pause — приостановить весь опрос
/resume — возобновить
/export — выгрузить находки в файл

<b>Прочее</b>
/cancel — прервать текущий диалог
/help — эта справка"""

HELP = f"""<b>Справка</b>

{COMMANDS}

{RULE}

<b>Как это работает</b>

Каждый запрос опрашивается по кругу. Первый опрос — <i>посевной</i>: я запоминаю
всё, что уже висит, и молчу. Дальше приходят только объявления, появившиеся
<b>после</b> создания запроса.

<b>Режимы опроса</b>
⚡ <b>Быстрый</b> — раз в {{fast}} с, только первая страница выдачи. Для случаев,
когда решает скорость.
🐢 <b>Полный</b> — раз в {{full}} мин, с обходом всех страниц. Медленнее, зато
ничего не теряется.

<b>Что уведомлять</b>
🆕 <b>Новые</b> — только те, которых раньше не было.
💰 <b>Новые + цена</b> — плюс изменение цены у уже известных объявлений.
🔄 <b>Новые + переподнятые + цена</b> — плюс ещё и старые, которые продавец
поднял в выдаче (вручную или платным автоподъёмом). Часто это признак, что
он готов торговаться.

<b>Стоп-слова</b> отсекают объявления по заголовку: «на запчастини», «подобово»,
«icloud». Проверяется вхождение подстроки, регистр не важен."""


def _price(watch: Watch) -> str:
    low, high = watch.filters.price_from, watch.filters.price_to
    if low is None and high is None:
        return "любая"
    fmt = lambda v: f"{v:,}".replace(",", " ")  # noqa: E731
    if low is None:
        return f"до {fmt(high)} грн"
    if high is None:
        return f"от {fmt(low)} грн"
    return f"{fmt(low)} – {fmt(high)} грн"


def _ago(moment: datetime | None) -> str:
    if moment is None:
        return "ещё не было"
    delta = datetime.now(UTC) - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 60 * 24:
        return f"{minutes // 60} ч назад"
    return moment.strftime("%d.%m в %H:%M")


def watch_label(watch: Watch) -> str:
    """Короткая подпись запроса для кнопок и списков.

    Пустая query -- режим «весь раздел без фразы» (см. models.Watch), подставляем
    название раздела вместо пустой строки, иначе кнопка выглядела бы сломанной.
    """
    if watch.query:
        return watch.query[:32]
    if watch.filters.category_id is not None:
        return "📂 " + categories.label(watch.filters.category_id)
    return "весь OLX"


def watch_card(watch: Watch) -> str:
    lines = [f"<b>#{watch.id} · {html.escape(watch_label(watch))}</b>"]

    if watch.filters.category_id is not None:
        lines.append(f"📂 {html.escape(categories.label(watch.filters.category_id))}")
    lines.append(f"💰 {_price(watch)}")
    if watch.filters.city_id is not None:
        city = geo.city_name(watch.filters.city_id) or f"город #{watch.filters.city_id}"
        lines.append(f"📍 {html.escape(city)}")
    elif watch.filters.region_id is not None:
        region = geo.region_name(watch.filters.region_id) or f"область #{watch.filters.region_id}"
        lines.append(f"🗺 {html.escape(region)}")
    if watch.filters.state is not None:
        lines.append(f"🏷 {'новое' if watch.filters.state == 'new' else 'б/у'}")
    if watch.filters.owner_type is not None:
        lines.append(f"👤 {'частники' if watch.filters.owner_type == 'private' else 'бизнес'}")
    if watch.stop_words:
        lines.append(f"🚫 {html.escape(', '.join(watch.stop_words))}")

    speed = "⚡ быстрый" if watch.poll_mode == PollMode.FAST else "🐢 полный"
    notify = {
        NotifyMode.NEW: "🆕 новые",
        NotifyMode.NEW_PRICE: "🆕 новые + 💰 изменение цены",
        NotifyMode.NEW_PUSHUP: "🆕 новые + 🔄 переподнятые + 💰 изменение цены",
    }[watch.notify_mode]
    state = "✅ активен" if watch.enabled else "⏸ на паузе"

    # Пустая строка разделяет «что ищем» и «как ищем»: без неё карточка читается
    # как один сплошной список, и режимы теряются среди фильтров.
    lines += ["", f"{speed} · {notify}", f"{state} · находки: {_ago(watch.last_found_at)}"]
    return "\n".join(lines)


def watch_list(watches: list[Watch]) -> str:
    if not watches:
        return "Запросов пока нет.\n\nДобавить: /add"
    return f"\n\n{RULE}\n\n".join(watch_card(w) for w in watches)


def status(watches: list[Watch], *, fast_interval: int) -> str:
    if not watches:
        return "Запросов нет, опрашивать нечего.\n\nДобавить: /add"

    active = [w for w in watches if w.enabled]
    polled = [w.last_polled_at for w in watches if w.last_polled_at]
    found = [w.last_found_at for w in watches if w.last_found_at]

    lines = [
        "<b>Состояние</b>",
        "",
        f"Запросов: <b>{len(watches)}</b>, активных: <b>{len(active)}</b>",
        f"Последний опрос: {_ago(max(polled) if polled else None)}",
        f"Последняя находка: {_ago(max(found) if found else None)}",
    ]
    if not active:
        lines += ["", "⏸ Опрос приостановлен целиком. Возобновить: /resume"]
    else:
        lines += ["", f"<i>Быстрые запросы опрашиваются раз в {fast_interval} с.</i>"]
    return "\n".join(lines)


def step(number: int, total: int, title: str, body: str) -> str:
    """Заголовок шага мастера.

    Номер шага показывается не для красоты: в мастере семь вопросов подряд, и без
    счётчика непонятно, это надолго или ещё пара нажатий.
    """
    return f"<b>Шаг {number} из {total} · {title}</b>\n\n{body}"
