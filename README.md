# Clinic SMM Manager

Автоматизированный SMM-менеджер для медицинской клиники.

Версия v4 умеет:

- проверять работоспособность backend через `/health`;
- хранить посты в PostgreSQL;
- создавать черновики постов;
- генерировать посты через OpenAI;
- принимать команды из Telegram-бота через webhook.

## Переменные Railway

Добавьте в Railway → backend service → Variables:

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=production
DATABASE_URL=postgresql://...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
TELEGRAM_BOT_TOKEN=123456:ABC...
ADMIN_TELEGRAM_ID=123456789
PUBLIC_BASE_URL=https://your-domain.up.railway.app
```

`ADMIN_TELEGRAM_ID` можно временно не задавать. Тогда бот будет отвечать любому пользователю. После теста лучше указать свой Telegram ID.

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Проверка API

```text
/health
/posts
/generate-post
/telegram/webhook-info
/telegram/set-webhook
```

## Как подключить Telegram webhook

1. Создайте Telegram-бота через BotFather.
2. Добавьте `TELEGRAM_BOT_TOKEN` в Railway Variables.
3. Добавьте `PUBLIC_BASE_URL`, например:

```env
PUBLIC_BASE_URL=https://clinic-smm-production.up.railway.app
```

4. Сделайте Redeploy backend-сервиса.
5. Откройте Swagger:

```text
https://your-domain.up.railway.app/docs
```

6. Выполните:

```text
POST /telegram/set-webhook
```

7. Проверьте:

```text
GET /telegram/webhook-info
```

8. Напишите боту:

```text
/start
```

Команды бота:

```text
/start
/help
/posts
/generate тема поста
```
