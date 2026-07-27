# Transfer Bot

Асинхронный Telegram-бот для одного или нескольких разрешённых пользователей. Бот опрашивает X через self-hosted RSSHub с фолбэком на Nitter, читает RSS СМИ, дедуплицирует новости, извлекает сущности через Anthropic и присылает уведомления с одной кнопкой: сделать пост.

## Стек

- Python 3.11+
- `aiogram` 3.x
- `aiosqlite`
- `APScheduler`
- `feedparser`
- `aiohttp`
- `anthropic`
- `pydantic-settings`
- `PyYAML`
- `rapidfuzz`

## Установка

1. Перейдите в каталог проекта:

```bash
cd /Users/macbookpro/Documents/New\ project\ 4/transfer_bot
```

2. Создайте и активируйте виртуальное окружение:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

3. Установите зависимости:

```bash
pip install -r requirements.txt
```

4. Подготовьте `.env`:

```bash
cp .env.example .env
```

5. Заполните переменные:

- `TELEGRAM_BOT_TOKEN`
- `OWNER_ID`
- `ALLOWED_USERS` (необязательно, список id через запятую)
- `ANTHROPIC_API_KEY`
- `TWITTER_AUTH_TOKEN`

## Как получить `TWITTER_AUTH_TOKEN`

1. Откройте X в браузере и авторизуйтесь.
2. Откройте DevTools.
3. Перейдите в `Application` → `Cookies` → `https://x.com`.
4. Найдите cookie `auth_token`.
5. Скопируйте значение в `.env` как `TWITTER_AUTH_TOKEN`.

Бот использует это значение для RSSHub-маршрута `/twitter/user/:username`. Если RSSHub недоступен, бот по очереди попробует инстансы Nitter из `config.yaml`.

## Запуск локально

```bash
python main.py
```

## Docker Compose

```bash
docker compose up --build
```

Снаружи порты не открываются. Бот работает через polling, RSSHub доступен только внутри compose-сети по адресу `http://rsshub:1200`.

## Команды

- `/start`
- `/sources`
- `/pause`
- `/resume`

## Тесты

```bash
pytest tests
```

## Файлы

- [main.py](/Users/macbookpro/Documents/New project 4/transfer_bot/main.py)
- [bot.py](/Users/macbookpro/Documents/New project 4/transfer_bot/bot.py)
- [pipeline.py](/Users/macbookpro/Documents/New project 4/transfer_bot/pipeline.py)
- [generator.py](/Users/macbookpro/Documents/New project 4/transfer_bot/generator.py)
- [sources.py](/Users/macbookpro/Documents/New project 4/transfer_bot/sources.py)
- [db.py](/Users/macbookpro/Documents/New project 4/transfer_bot/db.py)
- [reliability.py](/Users/macbookpro/Documents/New project 4/transfer_bot/reliability.py)
- [settings.py](/Users/macbookpro/Documents/New project 4/transfer_bot/settings.py)
