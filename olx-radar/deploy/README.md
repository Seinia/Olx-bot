# Развёртывание

Проверено на Ubuntu 24.04 с Docker 29 и Compose v5.

## Установка

```bash
mkdir -p /home/<user>/olx-monitor && cd /home/<user>/olx-monitor
```

С рабочей машины:

```bash
rsync -av --delete \
  --exclude .venv --exclude .git --exclude data/db --exclude __pycache__ \
  ./ <user>@СЕРВЕР:/home/<user>/olx-monitor/
scp .env <user>@СЕРВЕР:/home/<user>/olx-monitor/
```

На сервере:

```bash
mkdir -p data/db data/exports
# Процесс в контейнере работает под uid 1000, а не под root. Без выравнивания
# владельца SQLite получит "attempt to write a readonly database".
sudo chown -R 1000:1000 data/db data/exports

docker compose up -d --build
```

## Проверка

```bash
docker compose ps
docker compose logs -f
```

В Telegram бот должен отвечать на `/status`.

## Обновление

```bash
rsync -av --delete --exclude .venv --exclude .git --exclude data/db \
  ./ <user>@СЕРВЕР:/home/<user>/olx-monitor/
ssh <user>@СЕРВЕР 'cd /home/<user>/olx-monitor && docker compose up -d --build'
```

Пересборка проходит смоук-тест на этапе сборки: если справочники не доехали или
зависимости побились, образ не соберётся и старый контейнер продолжит работать.

## Что где лежит

| Путь | Что |
|---|---|
| `.env` | секреты, подключаются через `env_file`, в образ не попадают |
| `data/db/olx.db` | база: запросы, объявления, история цен |
| `data/exports/` | выгрузки CSV/JSON |
| `docker compose logs` | логи, ротация 10 МБ × 5 файлов |

Справочники разделов и городов лежат **внутри образа** — они часть сборки,
а не пользовательские данные. Обновляются пересборкой после
`scripts/refresh_categories.py` и `scripts/refresh_cities.py`.

## Важное

**База не переносится с рабочей машины.** В ней история показов: подложить чужую
или удалить — монитор заново засеет выдачу и замолчит до следующего действительно
нового объявления.

**Один процесс на один токен бота.** Telegram отдаёт обновления только одному
получателю: если бот параллельно запущен локально, сервер и рабочая машина будут
перехватывать команды друг у друга через раз.

**Живучесть держится на `restart: unless-stopped` и автозапуске самого Docker.**
Systemd-юнит (`olx-monitor.service`) остался в репозитории как альтернатива для
установки без Docker — но включать оба сразу нельзя, получите два процесса на один токен.
