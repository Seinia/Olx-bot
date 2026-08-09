import os

import pytest

LIVE_ENV = "OLX_LIVE_TESTS"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "telegram: шлёт настоящее сообщение владельцу в Telegram"
    )
    config.addinivalue_line("markers", "network: ходит на реальный olx.ua")


def pytest_collection_modifyitems(config, items):
    if os.environ.get(LIVE_ENV) == "1":
        return

    # Живые тесты по умолчанию пропускаем: telegram-тесты слали владельцу карточки при
    # каждом прогоне, а network-тесты набирают десятки обращений к olx.ua в день (R-1).
    skip = pytest.mark.skip(reason=f"живой тест; включить: {LIVE_ENV}=1")
    for item in items:
        if "telegram" in item.keywords or "network" in item.keywords:
            item.add_marker(skip)
