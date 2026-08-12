from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from loguru import logger

from olx import api, notify, storage
from olx.config import settings
from olx.errors import BlockedError, SchemaError, StorageError, TransportError
from olx.models import Listing, NotifyMode, PollMode, Watch
from olx.parse import apply_stop_words, matches_filters, parse_listing

Reason = Literal["new", "pushup", "price_change"]

NOTIFY_DELAY_SECONDS = 1.0
TRANSPORT_ALERT_AFTER = 3


@dataclass(frozen=True)
class Find:
    listing: Listing
    reason: Reason
    old_price: int | None = None


def select_new_from(
    watch: Watch,
    listings: list[Listing],
    *,
    seen: set[int],
    last_pushups: Mapping[int, datetime | None],
    # last_refresh_time (listing.refreshed_at) -- НЕ то же самое, что pushup_time.
    # Проверено на живом ответе API: у объявления с платным продвижением
    # (promotion.options: ["bundle_basic"]) именно last_refresh_time совпадало
    # с «Опубліковано сьогодні о 15:25» на странице, пока pushup_time стоял на
    # месте от последнего РУЧНОГО поднятия 11 дней назад. Автоподъём двигает
    # именно это поле, не pushup_time -- без него бот такие поднятия не видел.
    last_refreshes: Mapping[int, datetime | None] = {},
    # Дефолт безопасен: функция только читает из price_changed (.get/in), никогда
    # не мутирует. Без дефолта уже существующие вызовы (в т.ч. в тестах) ломались бы
    # каждый раз, когда про изменение цены знать не нужно.
    price_changed: Mapping[int, int | None] = {},
) -> list[Find]:
    finds: list[Find] = []

    for listing in listings:
        # Объявление раньше не встречалось этому watch.
        if listing.id not in seen:
            is_freshly_created = watch.created_at is None or listing.created_at >= watch.created_at

            if is_freshly_created:
                finds.append(Find(listing, "new"))
                continue

            # Объявление старше самого запроса, но это не значит, что оно неинтересно:
            # если его подняли или обновили уже ПОСЛЕ создания запроса, это тот же
            # сигнал активности продавца, что и обычный pushup -- то, что мы
            # встречаем объявление впервые именно сейчас, дела не меняет. Без этой
            # ветки бот молчал бы про старые объявления, поднятые уже во время
            # слежения, только потому что не видел их ДО подъёма.
            if watch.notify_mode is NotifyMode.NEW_PUSHUP and watch.created_at is not None:
                recently_pushed = (
                    listing.pushed_at is not None and listing.pushed_at >= watch.created_at
                )
                recently_refreshed = (
                    listing.refreshed_at is not None and listing.refreshed_at >= watch.created_at
                )
                if recently_pushed or recently_refreshed:
                    finds.append(Find(listing, "pushup"))
            continue

        # Уже известное объявление: что именно интересует, зависит от режима.
        if watch.notify_mode is NotifyMode.NEW:
            continue

        if watch.notify_mode is NotifyMode.NEW_PUSHUP:
            # Объявление было поднято -- вручную (pushup_time) или автоматически
            # платным продвижением (last_refresh_time). Уведомляем только при РОСТЕ
            # относительно уже сохранённой базы -- отсутствие базы (None) НЕ считается
            # поднятием, тем же принципом, что и record_price() для цены. Иначе любое
            # новое отслеживаемое поле (как last_refresh_at при этом самом апдейте)
            # при первом опросе после миграции пометило бы «поднятым» весь бэклог уже
            # отслеживаемых объявлений разом -- ровно то, что произошло на проде.
            pushed_up = False
            if listing.pushed_at is not None:
                previous_pushup = last_pushups.get(listing.id)
                if previous_pushup is not None and listing.pushed_at > previous_pushup:
                    pushed_up = True

            if not pushed_up and listing.refreshed_at is not None:
                previous_refresh = last_refreshes.get(listing.id)
                if previous_refresh is not None and listing.refreshed_at > previous_refresh:
                    pushed_up = True

            if pushed_up:
                finds.append(Find(listing, "pushup"))
                continue

        # Цена изменилась -- интересно в обоих режимах, где уже известные
        # объявления вообще рассматриваются (NEW_PRICE и NEW_PUSHUP).
        if listing.id in price_changed:
            finds.append(
                Find(
                    listing,
                    "price_change",
                    old_price=price_changed[listing.id],
                )
            )

    # Порядок выдачи OLX нестабилен -- сортируем сами.
    finds.sort(key=lambda f: (f.listing.created_at, f.listing.id))
    return finds


def select_new(
    watch: Watch,
    listings: list[Listing],
    *,
    price_changed: Mapping[int, int | None] = {},
) -> list[Find]:
    seen = storage.seen_ids(watch.id)

    last_pushups = {
        lst.id: storage.last_pushup(watch.id, lst.id)
        for lst in listings
        if lst.id in seen
    }
    last_refreshes = {
        lst.id: storage.last_refresh(watch.id, lst.id)
        for lst in listings
        if lst.id in seen
    }

    return select_new_from(
        watch,
        listings,
        seen=seen,
        last_pushups=last_pushups,
        last_refreshes=last_refreshes,
        price_changed=price_changed,
    )


class Poller:
    """Состояние устойчивости: счётчики неудач и текущий прокси."""

    def __init__(self, proxies: list[str] | None = None) -> None:
        self._proxies = proxies if proxies is not None else _load_proxies()
        self._index = 0
        self.failures = 0
        self.transport_failures = 0
        self.silence_alerted = False
        self.last_purge: datetime | None = None

    @property
    def proxy(self) -> str | None:
        if not settings.proxy_enabled or not self._proxies:
            return None
        return self._proxies[self._index % len(self._proxies)]

    def on_blocked(self) -> float:
        self.failures += 1
        # Меняем прокси даже при PROXY_ENABLED=false: раз дошло до блокировки,
        # прямой выход уже не работает.
        self._index += 1
        delay = settings.backoff_base**self.failures
        return min(delay, settings.backoff_max)

    def on_transport_error(self) -> int:
        self.transport_failures += 1
        return self.transport_failures

    def on_success(self) -> None:
        self.failures = 0
        self.transport_failures = 0


def _data_of(raw: dict) -> list[dict]:
    if not isinstance(raw, dict) or "data" not in raw:
        raise SchemaError("В ответе нет ключа 'data' -- структура API изменилась")
    data = raw["data"]
    if not isinstance(data, list):
        raise SchemaError(f"'data' не список, а {type(data).__name__}")
    return data


def _load_proxies() -> list[str]:
    path = settings.proxy_list_file
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def interval_for(watch: Watch) -> int:
    return (
        settings.poll_interval_full
        if watch.poll_mode is PollMode.FULL
        else settings.poll_interval_fast
    )


def is_due(watch: Watch, now: datetime) -> bool:
    if watch.last_polled_at is None:
        return True
    return (now - watch.last_polled_at).total_seconds() >= interval_for(watch)


def seconds_until_next(watches: list[Watch], now: datetime) -> float:
    if not watches:
        return float(settings.poll_interval_fast)
    waits = []
    for watch in watches:
        if watch.last_polled_at is None:
            return 0.0
        elapsed = (now - watch.last_polled_at).total_seconds()
        waits.append(interval_for(watch) - elapsed)
    return max(1.0, min(waits))


def poll_once(watch: Watch, *, proxy: str | None = None) -> list[Find]:
    if watch.poll_mode is PollMode.FULL:
        raw_items = api.search_all_pages(
            watch.query, watch.filters, max_pages=settings.full_max_pages, proxy=proxy
        )
    else:
        # Через _data_of, а не .get("data", []): иначе смена формата выглядела бы
        # как «ничего не нашлось» (R-8).
        raw_items = _data_of(api.search_raw(watch.query, watch.filters, proxy=proxy))

    listings = [parse_listing(item) for item in raw_items]
    listings = [lst for lst in listings if lst.is_active]
    # Одностраничная выдача тоже бывает с примесью мимо фильтров.
    before = len(listings)
    listings = [lst for lst in listings if matches_filters(lst, watch.filters)]
    if before != len(listings):
        logger.info(
            "Запрос #{}: отброшено {} объявлений мимо фильтров", watch.id, before - len(listings)
        )
    listings = apply_stop_words(listings, watch.stop_words)

    # Первый опрос посевной: «новое» значит «появилось после того, как я начал
    # следить», а не «вижу впервые».
    seeding = watch.last_polled_at is None

    # Цену фиксируем всегда, даже на посеве -- иначе у только что добавленного
    # запроса price_snapshots пуст, и карточка объявления показывает пустую цену
    # до следующего опроса (для full-режима это до 10 минут). А вот "было -> стало"
    # в уведомлении осмысленно только после посева: на посеве всё видим впервые,
    # сравнивать не с чем, и finds всё равно останутся пустыми.
    price_changed: dict[int, int | None] = {}

    for lst in listings:
        storage.upsert_listing(lst)
        old_price = storage.record_price(lst)
        if not seeding and old_price is not None:
            price_changed[lst.id] = old_price

    if seeding:
        finds = []
        logger.info(
            "Запрос #{}: посев, запомнено {} объявлений",
            watch.id,
            len(listings),
        )
    else:
        finds = select_new(
            watch,
            listings,
            price_changed=price_changed,
        )

    # Находки помечаются увиденными только после успешной отправки (_deliver):
    # иначе падение Telegram хоронит уведомление навсегда. Остальные -- сразу,
    # чтобы last_pushup_at не замирал на моменте первой встречи.
    pending = {find.listing.id for find in finds}
    for lst in listings:
        if lst.id not in pending:
            storage.mark_seen(watch.id, lst)

    # Пустая выдача не должна сжигать посев: иначе следующий опрос выплюнет
    # всю выдачу разом.
    if seeding and not listings:
        logger.warning("Запрос #{}: посев на пустой выдаче, повторю в следующий раз", watch.id)
        return finds

    now = datetime.now(UTC)
    fields: dict[str, object] = {"last_polled_at": now}
    if finds:
        fields["last_found_at"] = now
    storage.update_watch(watch.id, **fields)

    return finds


async def _alert(text: str) -> None:
    # Блокировка и обрыв сети коррелируют: сообщать о проблеме приходится ровно
    # тогда, когда канал уже может не работать.
    try:
        await notify.send_alert(text)
    except Exception as e:
        logger.error("Не удалось отправить алерт: {}: {}", type(e).__name__, e)


async def _deliver(finds: list[Find], watch: Watch) -> None:
    for index, find in enumerate(finds):
        try:
            await notify.send_listing(
                find.listing,
                watch,
                reason=find.reason,
                old_price=find.old_price,
            )
        except Exception as e:
            logger.error(
                "Не отправилось объявление {}: {}: {}", find.listing.id, type(e).__name__, e
            )
            continue
        storage.mark_seen(watch.id, find.listing)
        # Пачка вплотную ловит от Telegram 429 и теряет хвост.
        if index + 1 < len(finds):
            await asyncio.sleep(NOTIFY_DELAY_SECONDS)


async def _cycle(poller: Poller) -> None:
    now = datetime.now(UTC)
    for watch in storage.list_watches(enabled_only=True):
        if not is_due(watch, now):
            continue
        try:
            # poll_once синхронный целиком (curl_cffi, sleep, коммиты SQLite) --
            # в корутине напрямую он замораживал бота на секунды.
            finds = await asyncio.to_thread(poll_once, watch, proxy=poller.proxy)
        except BlockedError as e:
            delay = poller.on_blocked()
            logger.warning("Блокировка на запросе #{}: {}. Пауза {} с", watch.id, e, delay)
            if poller.failures == 1:
                await _alert(
                    f"⛔️ OLX блокирует запросы (#{watch.id}). Пауза {delay:.0f} с, меняю прокси."
                )
            await asyncio.sleep(delay)
            continue
        except SchemaError as e:
            # Поломка, а не отсутствие находок: снимаем запрос, чтобы не молотить
            # сломанный разбор по кругу.
            logger.error("Структура ответа не распознана на запросе #{}: {}", watch.id, e)
            storage.update_watch(watch.id, enabled=False)
            await _alert(f"🚨 Запрос #{watch.id} остановлен: OLX изменил формат ответа.\n{e}")
            continue
        except TransportError as e:
            failures = poller.on_transport_error()
            logger.warning("Сетевой сбой на запросе #{} ({}-й подряд): {}", watch.id, failures, e)
            if failures == TRANSPORT_ALERT_AFTER:
                await _alert(
                    f"📡 Сеть недоступна: {failures} сбоя подряд. Опрос продолжается вслепую."
                )
            continue
        except StorageError as e:
            # Замок снимется сам, перезапуск процесса тут ничего не чинит.
            logger.error("Хранилище недоступно на запросе #{}: {}", watch.id, e)
            await _alert(f"💾 База недоступна: {e}")
            continue

        poller.on_success()
        logger.info(
            "Запрос #{} опрошен ({}, раз в {} с): находок {}",
            watch.id,
            watch.poll_mode.value,
            interval_for(watch),
            len(finds),
        )
        await _deliver(finds, watch)


def silence_reference(watches: list[Watch]) -> datetime | None:
    """Момент, от которого отсчитывается тишина.

    Если находок не было ни разу -- берём создание запроса, иначе свежий запрос
    считался бы молчащим с начала эпохи и алерт летел бы сразу после /add.
    """
    found = [w.last_found_at for w in watches if w.last_found_at]
    if found:
        return max(found)
    created = [w.created_at for w in watches if w.created_at]
    return max(created) if created else None


async def check_silence(poller: Poller, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    watches = storage.list_watches(enabled_only=True)
    reference = silence_reference(watches)
    if reference is None:
        return False

    silent_for = now - reference
    if silent_for < timedelta(hours=settings.silence_alert_hours):
        poller.silence_alerted = False
        return False

    # Повторять каждый цикл нельзя: при цели в 25 секунд это 140 сообщений в час.
    if poller.silence_alerted:
        return False

    poller.silence_alerted = True
    hours = silent_for.total_seconds() / 3600
    await notify.send_alert(
        f"🔕 Находок нет {hours:.1f} ч по всем запросам. "
        "Либо на площадке правда тихо, либо монитор сломался и молчит."
    )
    return True


async def maybe_purge(poller: Poller, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if poller.last_purge is not None and now - poller.last_purge < timedelta(days=1):
        return False
    try:
        result = await asyncio.to_thread(
            storage.purge_old_history, retention_days=settings.history_retention_days, now=now
        )
    except StorageError as e:
        logger.error("Очистка истории не удалась: {}", e)
        return False
    poller.last_purge = now
    if result["listings"] or result["snapshots"]:
        logger.info(
            "Очистка истории: удалено объявлений {}, снимков цен {}",
            result["listings"],
            result["snapshots"],
        )
    return True


async def run_forever() -> None:
    storage.init_db(settings.db_path)
    poller = Poller()
    logger.info(
        "Монитор запущен: быстрый режим раз в {} с, полный раз в {} с",
        settings.poll_interval_fast,
        settings.poll_interval_full,
    )
    while True:
        await _cycle(poller)
        await check_silence(poller)
        await maybe_purge(poller)
        # Спим до ближайшего срока, а не фиксированный интервал: иначе полный режим
        # опрашивался бы с частотой быстрого -- до 10 обращений каждые 25 с (R-1).
        try:
            watches = storage.list_watches(enabled_only=True)
        except StorageError as e:
            logger.error("Не удалось прочитать запросы: {}", e)
            watches = []
        await asyncio.sleep(seconds_until_next(watches, datetime.now(UTC)))
