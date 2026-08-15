from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State as FSMState
from aiogram.fsm.state import StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from loguru import logger

from olx import api, categories, geo, storage, texts, wizard
from olx.config import settings
from olx.errors import SchemaError
from olx.models import Filters, NotifyMode, OwnerType, PollMode, State, Watch
from olx.parse import parse_search_response

router = Router()


class Add(StatesGroup):
    mode = FSMState()
    query = FSMState()
    category_browse = FSMState()
    price = FSMState()
    category = FSMState()
    city = FSMState()
    city_input = FSMState()
    region_input = FSMState()
    region_pick = FSMState()
    state = FSMState()
    owner = FSMState()
    stop_words = FSMState()
    poll_mode = FSMState()
    notify_mode = FSMState()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


class AllowedOnly(BaseMiddleware):
    """Пропускает только тех, кто есть в БД (storage.is_allowed).

    Источник истины -- таблица allowed_users, не .env: список правится командами
    /adduser, /removeuser в рантайме, без перезапуска бота. Чужие сообщения
    отбрасываются молча: ответ вроде «доступ запрещён» подтверждает, что бот
    живой и чем-то занят, и превращает случайного прохожего в интересующегося.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or not storage.is_allowed(user.id):
            logger.warning("Отброшено сообщение от чужого id={}", getattr(user, "id", "?"))
            return None
        return await handler(event, data)


def _is_owner(message: Message) -> bool:
    # Отдельная, более строгая проверка поверх AllowedOnly: управлять списком
    # доступа может только реальный владелец (settings.telegram_owner_id), а не
    # любой, кого туда когда-то добавили -- иначе любой приглашённый друг мог бы
    # добавить кого угодно ещё или, что хуже, удалить самого владельца.
    return message.from_user is not None and message.from_user.id == settings.telegram_owner_id


_fmt_watch = texts.watch_card


async def _step(message: Message, state: FSMContext, title: str, body: str, **kwargs: Any) -> None:
    """Очередной вопрос мастера со счётчиком шагов."""
    data = await state.get_data()
    number = data.get("step", 0) + 1
    await state.update_data(step=number)
    await message.answer(texts.step(number, data.get("steps", 8), title, body), **kwargs)


async def _step_same(
    message: Message, state: FSMContext, title: str, body: str, **kwargs: Any
) -> None:
    """Вопрос того же шага мастера, счётчик не двигается.

    Нужно для навигации по дереву категорий (Add.category_browse): дерево может
    быть сколь угодно глубоким, и если бы каждый уровень занимал отдельный номер,
    счётчик перегонял бы заявленный total уже на второй-третьей марке авто.
    """
    data = await state.get_data()
    text = texts.step(data.get("step", 1), data.get("steps", 8), title, body)
    await message.answer(text, **kwargs)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    commands = texts.COMMANDS
    if _is_owner(message):
        commands += f"\n\n{texts.OWNER_COMMANDS}"
    await message.answer(texts.START.format(commands=commands))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    help_text = texts.HELP.format(
        fast=settings.poll_interval_fast, full=settings.poll_interval_full // 60
    )
    if _is_owner(message):
        help_text += f"\n\n{texts.RULE}\n\n{texts.OWNER_COMMANDS}"
    await message.answer(help_text)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    await message.answer(
        texts.watch_list(storage.list_watches(user_id=message.from_user.id, enabled_only=False))
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer(
        texts.status(
            storage.list_watches(user_id=message.from_user.id, enabled_only=False),
            fast_interval=settings.poll_interval_fast,
        )
    )


# --- Управление доступом. Только для реального владельца (_is_owner), не для
# любого, кто есть в allowed_users -- см. комментарий у _is_owner выше.


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer(texts.users_list(storage.list_allowed_users(), settings.telegram_owner_id))


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return
    user_id = _parse_user_id_arg(command.args)
    if user_id is None:
        await message.answer(
            "Формат: <code>/adduser telegram_id</code>. "
            "Узнать свой id можно у @userinfobot."
        )
        return
    added = storage.add_allowed_user(user_id, added_by=message.from_user.id)
    if added:
        await message.answer(
            f"Пользователь <code>{user_id}</code> добавлен. "
            "Пусть напишет боту /start и заведёт свои запросы через /add."
        )
    else:
        await message.answer(f"Пользователь <code>{user_id}</code> уже был в списке.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return
    user_id = _parse_user_id_arg(command.args)
    if user_id is None:
        await message.answer("Формат: <code>/removeuser telegram_id</code>.")
        return
    if user_id == settings.telegram_owner_id:
        await message.answer("Нельзя убрать себя из списка — вы владелец бота.")
        return

    removed = storage.remove_allowed_user(user_id)
    if not removed:
        await message.answer(f"Пользователя <code>{user_id}</code> и так не было в списке.")
        return

    # Отзыв доступа блокирует НОВЫЕ сообщения боту, но не отменяет уже созданные
    # запросы -- notify.py шлёт уведомления по watch.user_id, а не по allowed_users
    # (бот технически может писать любому, кто хоть раз нажал /start, независимо
    # от нашего списка). Без явной паузы человек продолжал бы получать уведомления,
    # уже не имея возможности сам ничем управлять -- ставим на паузу вместо тихого
    # рассинхрона между "доступа нет" и "а находки всё равно идут".
    paused = 0
    for w in storage.list_watches(user_id=user_id, enabled_only=True):
        storage.update_watch(w.id, user_id=user_id, enabled=False)
        paused += 1

    text = f"Пользователь <code>{user_id}</code> удалён из списка доступа."
    if paused:
        text += f" Его активных запросов приостановлено: {paused}."
    await message.answer(text)


def _parse_user_id_arg(args: str | None) -> int | None:
    if args is None:
        return None
    token = args.strip().split()[0] if args.strip() else ""
    return int(token) if token.lstrip("-").isdigit() else None


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    for w in storage.list_watches(user_id=message.from_user.id, enabled_only=True):
        storage.update_watch(w.id, user_id=message.from_user.id, enabled=False)
    await message.answer("Опрос приостановлен. Возобновить: /resume")


async def _still_broken(watch: Watch) -> bool:
    """True, если запрос прямо сейчас всё ещё не разбирается.

    Схема watches не различает «на паузе по воле владельца» и «остановлен
    автоматом из-за SchemaError» -- колонки под это нет (и не заводим её здесь,
    см. отчёт T-27). Вместо чтения причины из БД перепроверяем вживую тем же
    способом, каким её обнаружил монитор: если разбор всё ещё падает, включать
    запрос молча нельзя, он тут же уйдёт на паузу заново на первом же опросе.
    """
    try:
        raw = await asyncio.to_thread(api.search_raw, watch.query, watch.filters)
        parse_search_response(raw)
    except SchemaError:
        return True
    except Exception as e:
        # Сеть/блокировка -- не то же самое, что поломка формата. Не блокируем
        # ручной /resume из-за временного сбоя транспорта, которого не было
        # при автоотключении.
        logger.warning(
            "Не удалось проверить запрос #{} перед /resume: {}: {}", watch.id, type(e).__name__, e
        )
        return False
    return False


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    paused = [
        w for w in storage.list_watches(user_id=message.from_user.id, enabled_only=False)
        if not w.enabled
    ]
    if not paused:
        await message.answer("Всё уже в работе.")
        return

    await message.answer(f"Проверяю приостановленные запросы ({len(paused)})…")
    resumed, broken = [], []
    for w in paused:
        (broken if await _still_broken(w) else resumed).append(w)

    for w in resumed:
        storage.update_watch(w.id, user_id=message.from_user.id, enabled=True)

    parts = []
    if resumed:
        parts.append(
            "Возобновлены:\n"
            + "\n".join(f"• #{w.id} — {html.escape(texts.watch_label(w))}" for w in resumed)
        )
    if broken:
        parts.append(
            "⚠️ Не включил — OLX всё ещё отдаёт форму, которую разбор не понимает "
            "(похоже, из-за этого их и остановило):\n"
            + "\n".join(f"• #{w.id} — {html.escape(texts.watch_label(w))}" for w in broken)
            + "\n\nВключить принудительно: /edit → «Пауза / возобновить» у нужного запроса."
        )
    await message.answer("\n\n".join(parts))


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(Add.mode)
    # База — 9 вопросов: способ поиска, фраза/раздел, цена, город, состояние,
    # продавец, стоп-слова, режим опроса, режим уведомлений. Раздел из результатов
    # поиска по фразе — необязательный десятый (см. add_query ниже).
    await state.update_data(step=0, steps=9, user_id=message.from_user.id)
    await _step(
        message,
        state,
        "Способ поиска",
        "Как ищем — по поисковой фразе (как на OLX) или сразу по разделу целиком, "
        "без фразы?",
        reply_markup=_kb(
            [
                [("🔤 По фразе", "mode:phrase")],
                [("📂 По разделу, без фразы", "mode:category")],
            ]
        ),
    )


@router.callback_query(Add.mode, F.data.startswith("mode:"))
async def add_mode(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    if value == "phrase":
        await call.message.edit_text("Способ: <b>по фразе</b>")
        await state.set_state(Add.query)
        await _step(
            call.message,
            state,
            "Запрос",
            "Что ищем? Напишите поисковую фразу так, как вводили бы её на OLX.",
        )
    else:
        # query="" -- маркер режима «весь раздел»: build_search_url() тогда не шлёт
        # параметр query вовсе, и работает чистый обход по category_id.
        await state.update_data(query="")
        await call.message.edit_text("Способ: <b>по разделу, без фразы</b>")
        await state.set_state(Add.category_browse)
        await _show_category_level(call.message, state, parent_id=None)
    await call.answer()


def _category_rows(parent_id: int | None) -> list[list[tuple[str, str]]]:
    nodes = categories.children(parent_id)
    rows = [[(n["name"], f"catb:{n['id']}")] for n in nodes]
    if parent_id is not None:
        rows.append([("✅ Весь этот раздел (со всеми подразделами)", f"catsel:{parent_id}")])
        back_to = categories.parent_id(parent_id)
        rows.append([("⬅️ Назад", f"catb:{back_to}" if back_to is not None else "catbroot")])
    return rows


async def _show_category_level(message: Message, state: FSMContext, parent_id: int | None) -> None:
    nodes = categories.children(parent_id)
    if not nodes and parent_id is not None:
        # Лист дерева без подразделов -- дальше сворачивать некуда, выбираем сразу.
        await _select_category(message, state, parent_id)
        return

    title = " › ".join(categories.path(parent_id)) if parent_id is not None else "Раздел"
    body = f"{title}\n\nВыберите раздел или подраздел из списка."
    data = await state.get_data()
    kwargs = {"reply_markup": _kb(_category_rows(parent_id))}
    if data.get("category_step_taken"):
        # Уже заняли свой номер шага при первом входе в дерево -- дальнейшее
        # погружение по подразделам его не двигает (см. _step_same).
        await _step_same(message, state, "Раздел", body, **kwargs)
    else:
        await state.update_data(category_step_taken=True)
        await _step(message, state, "Раздел", body, **kwargs)


async def _select_category(message: Message, state: FSMContext, category_id: int) -> None:
    await state.update_data(category_id=category_id)
    chosen = " › ".join(categories.path(category_id)) or f"раздел #{category_id}"
    await message.answer(f"Раздел: <b>{html.escape(chosen)}</b>")

    # Без фразы пробный поиск идёт сразу с category_id -- иначе он потянет вообще
    # весь каталог OLX, а не выбранный раздел (см. add_query -- там симметрично,
    # только фильтр появляется после выбора раздела, а не до).
    raw = await _preview(message, state, Filters(category_id=category_id))
    if raw is None:
        return
    if not raw:
        await message.answer("В этом разделе сейчас пусто на OLX. Начните заново: /add")
        await state.clear()
        return
    await state.update_data(preview_prices=_prices_by_category(raw))
    await _ask_price(message, state)


@router.callback_query(Add.category_browse, F.data.startswith("catb:"))
async def add_category_browse(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    await _show_category_level(call.message, state, parent_id=int(value))
    await call.answer()


@router.callback_query(Add.category_browse, F.data == "catbroot")
async def add_category_browse_root(call: CallbackQuery, state: FSMContext) -> None:
    await _show_category_level(call.message, state, parent_id=None)
    await call.answer()


@router.callback_query(Add.category_browse, F.data.startswith("catsel:"))
async def add_category_select(call: CallbackQuery, state: FSMContext) -> None:
    category_id = int(call.data.split(":", 1)[1])
    await _select_category(call.message, state, category_id)
    await call.answer()


async def _preview(message: Message, state: FSMContext, filters: Filters) -> list[dict] | None:
    data = await state.get_data()
    try:
        return api.search_raw(data["query"], filters).get("data", [])
    except Exception as e:
        logger.warning("Пробный поиск не удался: {}", e)
        await message.answer("Не смог проверить запрос — OLX не ответил. Попробуйте /add позже.")
        await state.clear()
        return None


def _prices_by_category(raw_items: list[dict]) -> list[tuple[int | None, int]]:
    out = []
    for item in raw_items:
        category_id = (item.get("category") or {}).get("id")
        for param in item.get("params") or []:
            if param.get("key") == "price":
                value = (param.get("value") or {}).get("value")
                if isinstance(value, int):
                    out.append((category_id, value))
    return out


@router.message(Add.query)
async def add_query(message: Message, state: FSMContext) -> None:
    try:
        query = wizard.validate_query(message.text or "")
    except ValueError as e:
        await message.answer(str(e))
        return

    await state.update_data(query=query)
    await message.answer(
        f"Ищем: <b>{html.escape(query)}</b>\nСмотрю, в какие разделы это попадает…"
    )

    # Раздел спрашивается ДО цены, и пробный поиск идёт без фильтров. Иначе цена сама
    # предрешает раздел: диапазон 9000-15000 грн по «квартира» оставляет одну аренду,
    # продажа отсекается ещё до вопроса, и выбора у владельца уже нет.
    raw = await _preview(message, state, Filters())
    if raw is None:
        return
    if not raw:
        await message.answer("По этому запросу на OLX сейчас ничего нет. Начните заново: /add")
        await state.clear()
        return

    # Цены из пробной выдачи нужны позже, чтобы посчитать пороги для кнопок. Тащим
    # их вместе с разделом: после выбора раздела пороги считаются только по нему,
    # иначе аренда и продажа усреднятся в бессмысленное число.
    await state.update_data(preview_prices=_prices_by_category(raw))

    found = geo.categories_from_raw(raw, limit=10)
    if len(found) <= 1:
        await _ask_price(message, state)
        return

    # Шаг с разделом появляется не всегда, поэтому общее число шагов известно
    # только здесь: без него счётчик показывал бы «9», а вопросов было бы десять.
    # База — 9 (способ поиска, фраза, цена, город, состояние, продавец, стоп-слова,
    # опрос, уведомления), этот шаг — десятый, поверх базы.
    await state.update_data(steps=10)
    rows = [[(categories.label(cid), f"cat:{cid}")] for cid, _, _ in found]
    rows.append([("Все разделы сразу", "cat:any")])
    await state.set_state(Add.category)
    await _step(
        message,
        state,
        "Раздел",
        "Запрос попал сразу в несколько разделов OLX. Какой нужен?",
        reply_markup=_kb(rows),
    )


@router.callback_query(Add.category, F.data.startswith("cat:"))
async def add_category(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    if value == "any":
        chosen = "все разделы"
    else:
        await state.update_data(category_id=int(value))
        chosen = " › ".join(categories.path(int(value))) or f"раздел #{value}"

    await call.message.edit_text(f"Раздел: <b>{html.escape(chosen)}</b>")
    await _ask_price(call.message, state)
    await call.answer()


async def _ask_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chosen = data.get("category_id")
    # Подраздел, а не точное совпадение: у «Легкові автомобілі» самого объявлений
    # нет, они лежат в подразделах-марках (см. categories.descendant_ids).
    wanted = categories.descendant_ids(chosen) if chosen is not None else None
    prices = [p for cat, p in data.get("preview_prices", []) if wanted is None or cat in wanted]

    rows = [
        [(label, f"price:{low or ''}:{high or ''}")]
        for label, low, high in wizard.price_presets(prices)
    ]
    rows.append([("Без ограничения", "price::")])

    await state.set_state(Add.price)
    await _step(
        message,
        state,
        "Цена",
        "Кнопки — по ценам, которые сейчас реально есть по этому запросу.\n\n"
        "Или впишите свой диапазон: <code>3000-8000</code>, <code>-8000</code>, "
        "<code>3000-</code>.",
        reply_markup=_kb(rows),
    )


@router.callback_query(Add.price, F.data.startswith("price:"))
async def add_price_button(call: CallbackQuery, state: FSMContext) -> None:
    _, low, high = call.data.split(":")
    await state.update_data(
        price_from=int(low) if low else None, price_to=int(high) if high else None
    )
    shown = f"{low or '…'}–{high or '…'} грн" if (low or high) else "без ограничения"
    await call.message.edit_text(f"Цена: <b>{shown}</b>")
    await _after_price(call.message, state)
    await call.answer()


@router.message(Add.price)
async def add_price(message: Message, state: FSMContext) -> None:
    try:
        low, high = wizard.parse_price_range(message.text or "")
    except ValueError as e:
        await message.answer(str(e))
        return
    await state.update_data(price_from=low, price_to=high)
    await _after_price(message, state)


async def _after_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    filters = Filters(
        price_from=data.get("price_from"),
        price_to=data.get("price_to"),
        category_id=data.get("category_id"),
    )
    raw = await _preview(message, state, filters)
    if raw is None:
        return
    if not raw:
        await message.answer(
            "С такой ценой в этом разделе сейчас ничего нет. Начните заново: /add"
        )
        await state.clear()
        return

    await _ask_city(message, state, raw)


def _pairs(items: list[tuple[int, str]]) -> list[list[tuple[str, str]]]:
    """Раскладывает кнопки в два столбца — иначе список городов уходит на экран вниз."""
    buttons = [(name, f"city:{cid}") for cid, name in items]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


async def _ask_city(message: Message, state: FSMContext, raw: list[dict]) -> None:
    # Города из выдачи -- самые релевантные, но выборка одностраничная, и крупного
    # города там может не оказаться вовсе. Поэтому к ним добавляются областные центры
    # из справочника, а на случай всего остального есть ручной ввод.
    from_results = [(cid, name) for cid, name, _ in geo.cities_from_raw(raw, limit=6)]
    seen = {cid for cid, _ in from_results}
    majors = [(cid, name) for cid, name in geo.major_cities() if cid not in seen][:6]

    rows = _pairs(from_results)
    if majors:
        rows += _pairs(majors)
    rows.append([("✍️ Ввести свой город", "city:manual")])
    rows.append([("🗺 Вся область (не один город)", "city:region")])
    rows.append([("🌍 Вся Украина", "city:any")])

    await state.set_state(Add.city)
    # Числа объявлений по городам не показываем: пробный поиск берёт одну страницу,
    # и «Київ (12)» означало бы 12 из 52 просмотренных, а не 12 в Киеве. Цифра,
    # выглядящая как факт, но им не являющаяся, хуже отсутствия цифры.
    await _step(
        message,
        state,
        "Город",
        "Сверху — города из выдачи, ниже — крупные. Или введите свой, либо отслеживайте "
        "сразу всю область.",
        reply_markup=_kb(rows),
    )


@router.callback_query(Add.city, F.data == "city:manual")
async def add_city_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Add.city_input)
    await call.message.edit_text("Введите название города.")
    await call.answer()


@router.callback_query(Add.city, F.data == "city:region")
async def add_city_region(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Add.region_input)
    await call.message.edit_text(
        "Введите область — например, «Львівська», «Одеська» или «Київська»."
    )
    await call.answer()


@router.message(Add.city_input)
async def add_city_typed(message: Message, state: FSMContext) -> None:
    matches = geo.find_cities(message.text or "")
    if not matches:
        await message.answer(
            "Такого города в справочнике нет. Попробуйте иначе — например, «Кам'янець» "
            "или «Бровар». Справочник обновляется скриптом, редкие сёла в него не попали."
        )
        return
    if len(matches) == 1:
        cid, name = matches[0]
        await state.update_data(city_id=cid)
        await _after_city(message, state, name)
        return

    await state.set_state(Add.city)
    await message.answer("Нашлось несколько. Какой?", reply_markup=_kb(_pairs(matches)))


@router.message(Add.region_input)
async def add_region_typed(message: Message, state: FSMContext) -> None:
    matches = geo.find_regions(message.text or "")
    if not matches:
        await message.answer(
            "Такой области в справочнике нет. Попробуйте иначе — например, «Львівська» "
            "или просто «Львів»."
        )
        return
    if len(matches) == 1:
        rid, name = matches[0]
        await state.update_data(region_id=rid)
        await _after_region(message, state, name)
        return

    await state.set_state(Add.region_pick)
    rows = [[(name, f"regsel:{rid}")] for rid, name in matches]
    await message.answer("Нашлось несколько. Какая?", reply_markup=_kb(rows))


@router.callback_query(Add.region_pick, F.data.startswith("regsel:"))
async def add_region_pick(call: CallbackQuery, state: FSMContext) -> None:
    rid = int(call.data.split(":", 1)[1])
    name = geo.region_name(rid) or f"область #{rid}"
    await state.update_data(region_id=rid)
    await call.message.edit_text(f"Область: <b>{html.escape(name)}</b>")
    await _goto_state_question(call.message, state)
    await call.answer()


async def _goto_state_question(message: Message, state: FSMContext) -> None:
    await state.set_state(Add.state)
    await message.answer(
        "Состояние товара?",
        reply_markup=_kb([[("Новое", "st:new"), ("Б/у", "st:used"), ("Любое", "st:any")]]),
    )


async def _after_city(message: Message, state: FSMContext, chosen: str) -> None:
    await message.answer(f"Город: <b>{html.escape(chosen)}</b>")
    await _goto_state_question(message, state)


async def _after_region(message: Message, state: FSMContext, chosen: str) -> None:
    await message.answer(f"Область: <b>{html.escape(chosen)}</b>")
    await _goto_state_question(message, state)


@router.callback_query(Add.city, F.data.startswith("city:"))
async def add_city(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    if value == "any":
        chosen = "вся Украина"
    else:
        await state.update_data(city_id=int(value))
        chosen = geo.city_name(int(value)) or f"город #{value}"

    await call.message.edit_text(f"Город: <b>{html.escape(chosen)}</b>")
    await _goto_state_question(call.message, state)
    await call.answer()


@router.callback_query(Add.state, F.data.startswith("st:"))
async def add_state(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    if value != "any":
        await state.update_data(state=value)
    await state.set_state(Add.owner)
    await call.message.edit_text(
        f"Состояние: <b>{ {'new': 'новое', 'used': 'б/у', 'any': 'любое'}[value] }</b>"
    )
    await call.message.answer(
        "Кто продавец?",
        reply_markup=_kb(
            [[("Только частники", "ow:private"), ("Только бизнес", "ow:business")],
             [("Не важно", "ow:any")]]
        ),
    )
    await call.answer()


@router.callback_query(Add.owner, F.data.startswith("ow:"))
async def add_owner(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    if value != "any":
        await state.update_data(owner_type=value)
    await state.set_state(Add.stop_words)
    await call.message.edit_text(
        f"Продавец: <b>{ {'private': 'частники', 'business': 'бизнес', 'any': 'любой'}[value] }</b>"
    )
    await call.message.answer(
        "Стоп-слова через запятую — объявления с ними в заголовке отсеются.\n"
        "Например: <code>на запчасти, не включается, icloud</code>\n"
        "Или <code>-</code>, если не нужно."
    )
    await call.answer()


@router.message(Add.stop_words)
async def add_stop_words(message: Message, state: FSMContext) -> None:
    await state.update_data(stop_words=wizard.parse_stop_words(message.text or ""))
    await state.set_state(Add.poll_mode)
    await message.answer(
        "Режим опроса?",
        reply_markup=_kb(
            [[("⚡ Быстрый", "pm:fast")], [("🐢 Полный", "pm:full")]]
        ),
    )


@router.callback_query(Add.poll_mode, F.data.startswith("pm:"))
async def add_poll_mode(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(poll_mode=call.data.split(":", 1)[1])
    await state.set_state(Add.notify_mode)
    await call.message.edit_text("Режим опроса выбран.")
    await call.message.answer(
        "О чём уведомлять?",
        reply_markup=_kb(
            [
                [("Только новые", "nm:new")],
                [("Новые + изменение цены", "nm:new_price")],
                [("Новые + переподнятые + цена", "nm:new_pushup")],
            ]
        ),
    )
    await call.answer()


@router.callback_query(Add.notify_mode, F.data.startswith("nm:"))
async def add_notify_mode(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    notify_mode = call.data.split(":", 1)[1]
    await state.clear()

    filters = Filters(
        price_from=data.get("price_from"),
        price_to=data.get("price_to"),
        city_id=data.get("city_id"),
        region_id=data.get("region_id"),
        category_id=data.get("category_id"),
        state=State(data["state"]) if data.get("state") else None,
        owner_type=OwnerType(data["owner_type"]) if data.get("owner_type") else None,
    )
    watch = storage.add_watch(
        data["query"],
        filters,
        PollMode(data.get("poll_mode", "fast")),
        NotifyMode(notify_mode),
        user_id=data["user_id"],
    )
    if data.get("stop_words"):
        storage.update_watch(watch.id, user_id=data["user_id"], stop_words=data["stop_words"])
        watch = next(
            w for w in storage.list_watches(user_id=data["user_id"], enabled_only=False)
            if w.id == watch.id
        )

    await call.message.edit_text("Готово.")
    await call.message.answer("Запрос сохранён и уже в очереди опроса:\n\n" + _fmt_watch(watch))
    await call.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил.")


class Edit(StatesGroup):
    price = FSMState()
    stop_words = FSMState()


def _watch_picker(action: str, user_id: int) -> InlineKeyboardMarkup | None:
    watches = storage.list_watches(user_id=user_id, enabled_only=False)
    if not watches:
        return None
    return _kb([[(f"#{w.id} — {texts.watch_label(w)}", f"{action}:{w.id}")] for w in watches])


@router.message(Command("del"))
async def cmd_del(message: Message) -> None:
    kb = _watch_picker("delask", message.from_user.id)
    if kb is None:
        await message.answer("Удалять нечего — запросов нет.")
        return
    await message.answer("Какой запрос удалить?", reply_markup=kb)


@router.callback_query(F.data.startswith("delask:"))
async def del_confirm(call: CallbackQuery) -> None:
    watch_id = int(call.data.split(":")[1])
    watches = storage.list_watches(user_id=call.from_user.id, enabled_only=False)
    watch = next((w for w in watches if w.id == watch_id), None)
    if watch is None:
        await call.message.edit_text("Этот запрос уже удалён.")
        await call.answer()
        return
    # Удаление каскадом сносит и историю показов -- переспрашиваем, а не удаляем сразу.
    await call.message.edit_text(
        f"Удалить запрос?\n\n{_fmt_watch(watch)}",
        reply_markup=_kb([[("Да, удалить", f"delyes:{watch_id}"), ("Отмена", "delno")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def del_do(call: CallbackQuery) -> None:
    storage.delete_watch(int(call.data.split(":")[1]), user_id=call.from_user.id)
    await call.message.edit_text("Запрос удалён.")
    await call.answer()


@router.callback_query(F.data == "delno")
async def del_cancel(call: CallbackQuery) -> None:
    await call.message.edit_text("Отменил, запрос на месте.")
    await call.answer()


@router.message(Command("edit"))
async def cmd_edit(message: Message) -> None:
    kb = _watch_picker("edit", message.from_user.id)
    if kb is None:
        await message.answer("Менять нечего — запросов нет. Создать: /add")
        return
    await message.answer("Какой запрос меняем?", reply_markup=kb)


@router.callback_query(F.data.startswith("edit:"))
async def edit_menu(call: CallbackQuery) -> None:
    watch_id = int(call.data.split(":")[1])
    # Строка запроса, город и раздел не редактируются: менять их -- это, по сути,
    # другой запрос, и накопленная история показов к нему уже не относится. Проще
    # и честнее создать новый через /add.
    await call.message.edit_text(
        "Что меняем?",
        reply_markup=_kb(
            [
                [("💰 Цену", f"ef:{watch_id}:price")],
                [("🚫 Стоп-слова", f"ef:{watch_id}:stop")],
                [("⚡ Режим опроса", f"ef:{watch_id}:poll")],
                [("🔔 Что уведомлять", f"ef:{watch_id}:notify")],
                [("⏸ Пауза / возобновить", f"ef:{watch_id}:toggle")],
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ef:"))
async def edit_field(call: CallbackQuery, state: FSMContext) -> None:
    _, raw_id, field = call.data.split(":")
    watch_id = int(raw_id)
    await state.update_data(edit_watch_id=watch_id, user_id=call.from_user.id)

    if field == "price":
        await state.set_state(Edit.price)
        await call.message.edit_text(
            "Новый диапазон: <code>3000-8000</code>, <code>-8000</code>, "
            "<code>3000-</code> или <code>-</code>, чтобы снять ограничение."
        )
    elif field == "stop":
        await state.set_state(Edit.stop_words)
        await call.message.edit_text(
            "Новые стоп-слова через запятую. <code>-</code> — очистить список."
        )
    elif field == "poll":
        await call.message.edit_text(
            "Режим опроса?",
            reply_markup=_kb([[("⚡ Быстрый", f"es:{watch_id}:poll_mode:fast")],
                              [("🐢 Полный", f"es:{watch_id}:poll_mode:full")]]),
        )
    elif field == "notify":
        await call.message.edit_text(
            "О чём уведомлять?",
            reply_markup=_kb(
                [
                    [("Только новые", f"es:{watch_id}:notify_mode:new")],
                    [("Новые + изменение цены", f"es:{watch_id}:notify_mode:new_price")],
                    [("Новые + переподнятые + цена", f"es:{watch_id}:notify_mode:new_pushup")],
                ]
            ),
        )
    elif field == "toggle":
        watches = storage.list_watches(user_id=call.from_user.id, enabled_only=False)
        watch = next(w for w in watches if w.id == watch_id)
        storage.update_watch(watch_id, user_id=call.from_user.id, enabled=not watch.enabled)
        await call.message.edit_text(
            "Запрос возобновлён." if not watch.enabled else "Запрос на паузе."
        )
    await call.answer()


@router.callback_query(F.data.startswith("es:"))
async def edit_set(call: CallbackQuery) -> None:
    _, raw_id, field, value = call.data.split(":")
    watch = storage.update_watch(int(raw_id), user_id=call.from_user.id, **{field: value})
    await call.message.edit_text("Готово.\n\n" + _fmt_watch(watch))
    await call.answer()


@router.message(Edit.price)
async def edit_price_typed(message: Message, state: FSMContext) -> None:
    try:
        low, high = wizard.parse_price_range(message.text or "")
    except ValueError as e:
        await message.answer(str(e))
        return
    data = await state.get_data()
    watch = storage.update_watch(
        data["edit_watch_id"], user_id=data["user_id"], price_from=low, price_to=high
    )
    await state.clear()
    await message.answer("Готово.\n\n" + _fmt_watch(watch))


@router.message(Edit.stop_words)
async def edit_stop_words_typed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    watch = storage.update_watch(
        data["edit_watch_id"],
        user_id=data["user_id"],
        stop_words=wizard.parse_stop_words(message.text or ""),
    )
    await state.clear()
    await message.answer("Готово.\n\n" + _fmt_watch(watch))


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    watches = storage.list_watches(user_id=message.from_user.id, enabled_only=False)
    rows = [[(f"#{w.id} — {texts.watch_label(w)}", f"exp:{w.id}")] for w in watches]
    rows.append([("Все объявления", "exp:all")])
    await message.answer("Что выгружаем?", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("exp:"))
async def export_pick_format(call: CallbackQuery) -> None:
    # Идентификатор запроса едет прямо в callback_data следующего шага, а не через
    # FSMContext -- тот же приём, что у ef:/es: выше: короче и не текает между шагами.
    watch_ref = call.data.split(":", 1)[1]
    await call.message.edit_text(
        "В каком формате?",
        reply_markup=_kb(
            [[("CSV (Excel)", f"expfmt:{watch_ref}:csv"), ("JSON", f"expfmt:{watch_ref}:json")]]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("expfmt:"))
async def export_send(call: CallbackQuery) -> None:
    _, watch_ref, fmt = call.data.split(":")
    watch_id = None if watch_ref == "all" else int(watch_ref)
    path = storage.export(fmt, watch_id, user_id=call.from_user.id)
    await call.message.edit_text("Готово, отправляю файл…")
    await call.message.answer_document(FSInputFile(path))
    await call.answer()


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(AllowedOnly())
    dp.include_router(router)
    return dp


async def run_bot() -> None:
    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await build_dispatcher().start_polling(bot)
