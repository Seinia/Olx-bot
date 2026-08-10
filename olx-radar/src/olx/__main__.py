import asyncio
import sys

from loguru import logger

from olx import bot, monitor, storage
from olx.config import settings


async def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    storage.init_db(settings.db_path)
    logger.info("БД: {}", settings.db_path)
    # Бот и монитор живут в одном процессе: они делят одну SQLite-базу, и разносить
    # их по процессам значило бы разбираться с блокировками ради нулевой выгоды.
    await asyncio.gather(bot.run_bot(), monitor.run_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен вручную")