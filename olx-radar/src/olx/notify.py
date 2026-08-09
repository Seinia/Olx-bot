from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal

import httpx
from loguru import logger

from olx.config import settings
from olx.errors import OlxError
from olx.models import Listing, Watch
from olx.texts import watch_label

TELEGRAM_API = "https://api.telegram.org/bot{token}"
REQUEST_TIMEOUT = 30.0

# Подпись под фото в Telegram ограничена 1024 символами, и превышение -- это отказ
# всего sendPhoto, а не обрезка. Остальное в подписи занимает ~250, берём запас.
DESCRIPTION_LIMIT = 600
MEDIA_GROUP_LIMIT = 10

# Свежее объявление доходит за ~15-30 мин (лаг платформы + опрос). Больший разрыв
# значит, что в выдачу оно вошло позже публикации -- обычно после смены цены.
LATE_DETECTION_SECONDS = 3 * 3600

_BR_TAG = re.compile(r"<br\s*/?>\s*", re.IGNORECASE)

# Telegram официально держит ~1 сообщение в секунду на один чат и наказывает за
# нарушение растущими паузами (виденный retry_after доходил до 500+ секунд). Запас
# над формальным лимитом, потому что сетевой джиттер сам по себе может столкнуть
# два вызова ближе секунды друг к другу.
MIN_REQUEST_INTERVAL_SECONDS = 1.1

# Общий на весь процесс, а не на одно объявление: без этого троттлинг внутри
# одного send_listing (фоллбэк по фото при отказе sendMediaGroup) никак не защищал
# от всплеска между РАЗНЫМИ объявлениями и запросами -- 429 ловился именно там.
_rate_limit_lock = asyncio.Lock()
_next_allowed_at = 0.0


async def _throttle() -> None:
    """Ждать перед следующим обращением к Telegram, чтобы не поймать 429."""
    global _next_allowed_at
    async with _rate_limit_lock:
        now = time.monotonic()
        wait = _next_allowed_at - now
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        _next_allowed_at = now + MIN_REQUEST_INTERVAL_SECONDS


async def _postpone(seconds: float) -> None:
    """Telegram попросил подождать -- следующий вызов _throttle() ждёт не меньше этого.

    Не ретраим внутри самой функции: следующий, кто бы он ни был (следующее фото
    этого объявления, следующая находка, следующий запрос), и так пройдёт через
    _throttle() и упрётся в этот же дедлайн. Ретрай на месте только продлил бы
    бан повторным 429, если Telegram уже недоволен всем ботом, а не одним вызовом.
    """
    global _next_allowed_at
    async with _rate_limit_lock:
        _next_allowed_at = max(_next_allowed_at, time.monotonic() + seconds)


def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        return float(response.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        # Telegram обычно кладёт retry_after и в заголовок, и в тело -- если нет
        # ни там, ни там, подстраховываемся консервативной паузой, не нулевой.
        return 5.0


class _DescriptionTextParser(HTMLParser):
    """Извлекает текст из HTML, который возвращает OLX в description."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


class NotifyError(OlxError):
    """Telegram отверг и sendPhoto, и sendMessage -- уведомление не ушло никуда."""


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/{method}"


def _price_line(listing: Listing) -> str:
    if listing.price is None:
        return "Договорная"
    price = f"{listing.price:,}".replace(",", " ")
    line = f"{price} {listing.currency or ''}".strip()
    return f"{line} (торг)" if listing.negotiable else line


def _short_description(text: str) -> str:
    # API OLX возвращает description как HTML: чаще всего <br />, иногда также
    # сущности и другие теги. Telegram не поддерживает <br>, а экранирование
    # без предварительной очистки показывает этот тег в сообщении буквально.
    # Сначала превращаем переносы в \n, затем оставляем только текст и лишь
    # после этого экранируем результат при сборке HTML-подписи.
    text = _BR_TAG.sub("\n", text)
    parser = _DescriptionTextParser()
    parser.feed(text)
    parser.close()
    text = parser.text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= DESCRIPTION_LIMIT:
        return text
    cut = text[:DESCRIPTION_LIMIT]
    # Рвём по последнему пробелу, чтобы обрезка не приходилась на середину слова.
    space = cut.rfind(" ")
    return (cut[:space] if space > DESCRIPTION_LIMIT // 2 else cut).rstrip(" ,.;:-") + "…"


def _humanize(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    days = hours // 24
    return f"{days} дн" if days < 30 else f"{days // 30} мес"


def _age(moment: datetime, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    seconds = (now - moment).total_seconds()
    return "только что" if seconds < 60 else f"{_humanize(seconds)} назад"


def _when_lines(
    listing: Listing,
    reason: Literal["new", "pushup", "price_change"],
    *,
    now: datetime | None = None,
) -> list[str]:
    now = now or datetime.now(UTC)
    if reason == "new":
        gap = (now - listing.created_at).total_seconds()
        if gap <= LATE_DETECTION_SECONDS:
            return [f"🕐 {listing.created_at:%d.%m %H:%M} · {_age(listing.created_at, now=now)}"]
        # Опубликовано давно, а в выдачу вошло только сейчас -- обычно из-за смены цены.
        return [
            f"🕐 опубликовано {listing.created_at:%d.%m %H:%M}",
            f"👀 замечено спустя {_humanize(gap)} после публикации — возможно, изменилась цена",
        ]
    if reason == "price_change":
        age = _age(listing.created_at, now=now)
        lines = [f"🕐 опубликовано {listing.created_at:%d.%m.%Y} · {age}"]

        if listing.pushed_at is not None:
            when = listing.pushed_at
            lines.append(f"🔄 поднято {when:%d.%m %H:%M} · {_age(when, now=now)}")

        return lines

    age = _age(listing.created_at, now=now)
    lines = [f"🕐 опубликовано {listing.created_at:%d.%m.%Y} · {age}"]
    if listing.pushed_at is not None:
        when = listing.pushed_at
        lines.append(f"🔄 поднято {when:%d.%m %H:%M} · {_age(when, now=now)}")
    return lines


def _caption(
    listing: Listing,
    watch: Watch,
    *,
    reason: Literal["new", "pushup", "price_change"],
    old_price: int | None = None,
) -> str:
    # title/city/query/description приходят с OLX или введены владельцем в боте --
    # не наша разметка, экранируем перед вставкой в текст с parse_mode=HTML.
    title = html.escape(listing.title)
    city = html.escape(listing.city_name)
    query = html.escape(watch_label(watch))
    # Один и тот же listing может прийти по нескольким watch (дедуп в contracts.md
    # намеренно попарный) -- без имени запроса непонятно, почему пришла карточка.
    mark = {
        "new": "🆕 Новое",
        "pushup": "🔄 Переподнято",
        "price_change": "💰 Изменилась цена",
    }[reason]

    body = [
        f"{mark} · запрос «{query}»",
        "",
        f"<b>{title}</b>",
        "",
    ]

    if reason == "price_change" and old_price is not None:
        old_price_text = f"{old_price:,}".replace(",", " ")
        new_price_text = _price_line(listing)

        body.append(
            f"💰 Цена: {old_price_text} {listing.currency or ''} → {new_price_text}"
        )
    else:
        body.append(f"💰 {_price_line(listing)}")

    body += [
        f"📍 {city}",
        *_when_lines(listing, reason),
    ]
    if listing.description.strip():
        body += ["", html.escape(_short_description(listing.description))]
    body += ["", f'<a href="{listing.url}">Открыть на OLX</a>']
    return "\n".join(body)


async def _send_photo(
    client: httpx.AsyncClient, chat_id: int, photo_url: str, caption: str | None = None
) -> bool:
    data: dict[str, str | int] = {"chat_id": chat_id, "photo": photo_url}
    if caption is not None:
        data.update({"caption": caption, "parse_mode": "HTML"})
    await _throttle()
    response = await client.post(
        _api_url("sendPhoto"),
        data=data,
    )
    if response.status_code == 429:
        await _postpone(_retry_after_seconds(response))
    return response.status_code == 200


async def _send_media_group(
    client: httpx.AsyncClient, chat_id: int, photo_urls: tuple[str, ...], caption: str | None
) -> bool:
    media: list[dict[str, str]] = [{"type": "photo", "media": url} for url in photo_urls]
    if caption is not None:
        media[0].update({"caption": caption, "parse_mode": "HTML"})
    await _throttle()
    response = await client.post(
        _api_url("sendMediaGroup"),
        data={"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
    )
    if response.status_code == 429:
        await _postpone(_retry_after_seconds(response))
    return response.status_code == 200


async def _send_message(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    await _throttle()
    response = await client.post(
        _api_url("sendMessage"),
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    if response.status_code == 429:
        await _postpone(_retry_after_seconds(response))
    if response.status_code != 200:
        raise NotifyError(
            f"Telegram sendMessage вернул {response.status_code}: {response.text[:300]}"
        )


async def send_listing(
    listing: Listing,
    watch: Watch,
    *,
    reason: Literal["new", "pushup", "price_change"],
    old_price: int | None = None,
) -> None:
    caption = _caption(
        listing,
        watch,
        reason=reason,
        old_price=old_price,
    )
    chat_id = settings.telegram_owner_id
    photo_urls = listing.photo_urls or (
        (listing.photo_url,) if listing.photo_url is not None else ()
    )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        if len(photo_urls) == 1:
            if await _send_photo(client, chat_id, photo_urls[0], caption):
                return
            # Ссылка на фото -- шаблон, подставленный в parse.py (контракты, п.1.2).
            # OLX может сменить его формат или файл может исчезнуть -- само
            # уведомление из-за этого теряться не должно.
            logger.warning(f"sendPhoto для {listing.id} отклонён Telegram, фоллбэк на sendMessage")
        elif len(photo_urls) > 1:
            caption_sent = False
            for start in range(0, len(photo_urls), MEDIA_GROUP_LIMIT):
                group = photo_urls[start : start + MEDIA_GROUP_LIMIT]
                group_caption = None if caption_sent else caption
                if await _send_media_group(client, chat_id, group, group_caption):
                    caption_sent = caption_sent or group_caption is not None
                    continue

                logger.warning(
                    "sendMediaGroup для {} отклонён Telegram, отправляю фото по одному", listing.id
                )
                for photo_url in group:
                    photo_caption = None if caption_sent else caption
                    if await _send_photo(client, chat_id, photo_url, photo_caption):
                        caption_sent = caption_sent or photo_caption is not None
                    else:
                        logger.warning("sendPhoto для {} отклонён Telegram", listing.id)
            if caption_sent:
                return
        await _send_message(client, chat_id, caption)


async def send_alert(text: str) -> None:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        await _send_message(client, settings.telegram_owner_id, text)
