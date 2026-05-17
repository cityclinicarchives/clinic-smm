# Clinic SMM Manager

FastAPI backend для автоматизированного SMM-менеджера медицинской клиники.

## Версия v5

Добавлено:

- генерация постов через OpenAI;
- сохранение постов в PostgreSQL;
- Telegram-бот для ручного управления;
- просмотр поста по ID;
- одобрение поста;
- отклонение поста;
- ручное редактирование поста;
- ИИ-редактирование поста по инструкции.

## Основные endpoints

- `GET /health` — проверка работы сервера;
- `GET /posts` — список постов;
- `GET /posts/{post_id}` — открыть пост;
- `POST /generate-post` — создать пост через OpenAI;
- `POST /posts/{post_id}/approve` — одобрить пост;
- `POST /posts/{post_id}/reject` — отклонить пост;
- `PATCH /posts/{post_id}/edit` — заменить текст вручную;
- `POST /posts/{post_id}/rewrite` — отредактировать текст через ИИ;
- `POST /telegram/set-webhook` — подключить Telegram webhook.

## Telegram команды

- `/start` — проверить бота;
- `/help` — список команд;
- `/generate тема` — создать пост;
- `/posts` — последние посты;
- `/post ID` — посмотреть пост;
- `/approve ID` — одобрить пост;
- `/reject ID` — отклонить пост;
- `/edit ID новый текст` — заменить текст вручную;
- `/rewrite ID инструкция` — отредактировать через ИИ.

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
```
