from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from olx.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MIN_POLL_INTERVAL = 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    telegram_owner_id: int

    db_path: Path = Path("data/olx.db")

    poll_interval_fast: int = 25
    poll_interval_full: int = 600
    full_max_pages: int = 10
    silence_alert_hours: int = 12
    history_retention_days: int = 30

    proxy_enabled: bool = False
    proxy_list_file: Path = Path("proxies.txt")

    backoff_base: float = 2.0
    backoff_max: int = 1800
    log_level: str = "INFO"

    @field_validator("db_path", "proxy_list_file")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        # Пути в .env заданы относительно корня проекта. Под systemd рабочий каталог
        # задаётся юнитом и не обязан совпадать с корнем — без этого БД создастся
        # не там, где её ищет остальной код.
        return v if v.is_absolute() else PROJECT_ROOT / v

    @field_validator("poll_interval_fast")
    @classmethod
    def _not_too_fast(cls, v: int) -> int:
        # S-07: за 10 минут наблюдения выдача не обновилась ни разу. Опрос чаще
        # 20 с не приносит свежести, только жжёт лимит.
        if v < MIN_POLL_INTERVAL:
            raise ValueError(
                f"poll_interval_fast={v} меньше {MIN_POLL_INTERVAL} с. "
                "Более частый опрос не даёт выигрыша в скорости — см. FINDINGS.md, S-07."
            )
        return v

    @field_validator("history_retention_days")
    @classmethod
    def _positive_retention(cls, v: int) -> int:
        if v < 1:
            raise ValueError("history_retention_days должен быть не меньше 1")
        return v


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        missing = [str(err["loc"][0]).upper() for err in e.errors() if err["type"] == "missing"]
        if missing:
            raise ConfigError(
                "В .env не заданы обязательные переменные: " + ", ".join(missing)
            ) from e
        problems = "; ".join(f"{str(err['loc'][0]).upper()}: {err['msg']}" for err in e.errors())
        raise ConfigError(f"Некорректная конфигурация — {problems}") from e


settings = load_settings()
