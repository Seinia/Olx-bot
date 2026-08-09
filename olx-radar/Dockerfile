FROM python:3.12-slim

# Время публикации приходит с +03:00, в UTC логи читать неудобно.
ENV TZ=Europe/Kyiv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/ ./scripts/
# Справочники нужны в рантайме: без них падают /add и /list.
COPY data/reference/ ./data/reference/

RUN pip install --no-cache-dir --no-deps -e .

RUN useradd --create-home --uid 1000 olx && chown -R olx:olx /app
USER olx

# Промахи с раскладкой файлов должны всплывать на сборке, а не при первом /add.
# Токен фиктивный: конфиг читается на импорте olx.bot, настоящий приходит на запуске.
RUN TELEGRAM_BOT_TOKEN=build-check TELEGRAM_OWNER_ID=1 \
    python -c "import olx.bot, olx.monitor; from olx import categories, geo; \
    assert len(categories._tree()) > 2000 and len(geo._cities()) > 300; \
    print('смоук-тест пройден')"

CMD ["python", "-m", "olx"]
