# Clinic SMM Manager v6

Backend для AI-SMM менеджера клиники.

## Возможности v6

- Проверка `/health`
- PostgreSQL
- Создание черновиков постов
- Генерация постов через OpenAI
- Telegram-бот для ручного управления
- Просмотр постов
- Ручное редактирование
- AI-редактирование
- Одобрение и отклонение
- Публикация одобренного поста в тестовую Telegram-группу

## Railway Variables

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=production
DATABASE_URL=postgresql://...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_ID=...
PUBLIC_BASE_URL=https://твой-домен.up.railway.app
TELEGRAM_PUBLISH_CHAT_ID=-1001234567890
```

`TELEGRAM_PUBLISH_CHAT_ID` — ID тестовой группы, куда бот будет публиковать одобренные посты.
Позже сюда можно поставить ID настоящего Telegram-канала клиники.

## Команды Telegram-бота

```text
/start или /help
/generate тема
/posts
/post ID
/approve ID
/reject ID
/edit ID новый текст
/rewrite ID инструкция
/publish ID
```

## Правильный процесс

```text
/generate тема
/post ID
/rewrite ID инструкция или /edit ID текст
/approve ID
/publish ID
```

Публиковать можно только посты со статусом `approved`.
После публикации статус меняется на `published`.
