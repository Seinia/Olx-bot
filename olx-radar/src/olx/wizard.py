"""Разбор пользовательского ввода в мастере /add.

Вынесено из bot.py, потому что диалоговые обработчики aiogram тестировать дорого,
а сами правила разбора — дёшево. Здесь всё чистое, без сети и состояния.
"""

from __future__ import annotations

import re

SKIP = {"-", "—", "нет", "любая", "любые", "пропустить", "skip"}

_PRICE_RE = re.compile(r"^\s*(\d+)?\s*[-–—:]\s*(\d+)?\s*$")


def parse_price_range(text: str) -> tuple[int | None, int | None]:
    """«3000-8000», «-8000», «3000-», «-» -> границы. Ошибка -> ValueError."""
    if text.strip().lower() in SKIP:
        return None, None

    m = _PRICE_RE.match(text)
    if m:
        low = int(m.group(1)) if m.group(1) else None
        high = int(m.group(2)) if m.group(2) else None
    elif text.strip().isdigit():
        low, high = int(text.strip()), None
    else:
        raise ValueError(
            "Не понял диапазон. Примеры: «3000-8000», «-8000», «3000-», «-» чтобы пропустить."
        )

    if low is not None and high is not None and low > high:
        raise ValueError(f"Нижняя граница {low} больше верхней {high}.")
    return low, high


def parse_stop_words(text: str) -> list[str]:
    if text.strip().lower() in SKIP:
        return []
    words = [w.strip().lower() for w in text.split(",")]
    # Порядок сохраняем, дубликаты убираем: список показывается пользователю в /list,
    # и «icloud, icloud» выглядит как ошибка бота, а не как его ввод.
    seen: set[str] = set()
    result = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            result.append(w)
    return result


def _round_nice(value: int) -> int:
    """Округляет до двух значащих цифр: 12 480 -> 12 000, 850 -> 850."""
    if value < 100:
        return value
    step = 10 ** (len(str(value)) - 2)
    return max(step, (value // step) * step)


def price_presets(prices: list[int]) -> list[tuple[str, int | None, int | None]]:
    """Пороги «до / между / от», посчитанные по третям реальных цен выдачи.

    Фиксированные пресеты тут не годятся: «до 5000» осмысленно для телефона и
    бессмысленно для квартиры. Пороги считаются от того, что реально есть по запросу.
    """
    values = sorted(p for p in prices if p and p > 0)
    if len(values) < 6:
        return []

    low = _round_nice(values[len(values) // 3])
    high = _round_nice(values[2 * len(values) // 3])
    if low >= high:
        return []

    return [
        (f"до {low:,}".replace(",", " "), None, low),
        (f"{low:,}–{high:,}".replace(",", " "), low, high),
        (f"от {high:,}".replace(",", " "), high, None),
    ]


def validate_query(text: str) -> str:
    query = text.strip()
    if not query:
        raise ValueError("Пустой запрос.")
    if len(query) > 100:
        raise ValueError("Слишком длинный запрос — не больше 100 символов.")
    return query
