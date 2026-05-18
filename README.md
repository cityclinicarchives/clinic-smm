# Clinic SMM Manager v7

AI-SMM backend для клиники: генерация постов, редактирование, согласование, генерация изображений и публикация в тестовую Telegram-группу.

## Что умеет v7

- `/generate тема` — создать текстовый пост.
- `/generate_full тема` — создать пост и изображение.
- `/posts` — показать последние посты.
- `/post ID` — открыть пост.
- `/rewrite ID инструкция` — отредактировать пост через ИИ.
- `/edit ID новый текст` — заменить текст вручную.
- `/image ID` — сгенерировать/заменить изображение.
- `/image ID инструкция` — сгенерировать изображение с уточнением.
- `/approve ID` — одобрить пост.
- `/reject ID` — отклонить пост.
- `/publish ID` — опубликовать одобренный пост в тестовую группу.

## Railway Variables

Обязательные переменные:

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=production
DATABASE_URL=postgresql://...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_IMAGE_MODEL=gpt-image-1
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_ID=...
PUBLIC_BASE_URL=https://your-domain.up.railway.app
TELEGRAM_PUBLISH_CHAT_ID=-1001234567890
GENERATED_IMAGES_DIR=storage/generated_images
```

## Проверка

После деплоя открой:

```text
https://твой-домен.up.railway.app/health
```

Swagger:

```text
https://твой-домен.up.railway.app/docs
```

Telegram webhook:

```text
POST /telegram/set-webhook
```

## Важно

В v7 изображения хранятся в файловой системе Railway. Это подходит для теста, но для production позже нужно подключить S3-совместимое хранилище.

## v8: кнопки под постами

В Telegram добавлены inline-кнопки под каждым постом:

- ✅ Одобрить
- 🚀 Опубликовать
- 🖼 Картинка
- ❌ Отклонить
- 👁 Показать пост
- ✏️ Как редактировать

После обновления нужно заново выполнить в Swagger:

```text
POST /telegram/set-webhook
```

Это нужно, потому что webhook теперь принимает не только обычные сообщения, но и нажатия кнопок (`callback_query`).
