import pytest

import olx.config as cfg
from olx.config import PROJECT_ROOT, Settings
from olx.errors import ConfigError


def test_settings_load_from_env_file():
    s = Settings()
    assert isinstance(s.telegram_owner_id, int) and s.telegram_owner_id > 0
    assert s.telegram_bot_token


def test_missing_required_vars_raise_config_error(tmp_path, monkeypatch):
    # Переменные окружения перекрывают env_file, поэтому чистим их — иначе тест
    # пройдёт на машине разработчика и упадёт в CI, или наоборот.
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"):
        monkeypatch.delenv(name, raising=False)

    env = tmp_path / "empty.env"
    env.write_text("DB_PATH=./data/olx.db\n", encoding="utf-8")
    monkeypatch.setitem(cfg.Settings.model_config, "env_file", env)

    with pytest.raises(ConfigError) as e:
        cfg.load_settings()

    assert "TELEGRAM_BOT_TOKEN" in str(e.value)
    assert "TELEGRAM_OWNER_ID" in str(e.value)


def test_relative_paths_resolve_against_project_root():
    s = Settings(db_path="data/olx.db")
    assert s.db_path == PROJECT_ROOT / "data" / "olx.db"


def test_poll_interval_below_floor_rejected():
    with pytest.raises(Exception) as e:
        Settings(poll_interval_fast=5)
    assert "S-07" in str(e.value)
