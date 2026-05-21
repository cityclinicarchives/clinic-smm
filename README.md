# Clinic SMM Manager v18

Версия v18 добавляет новый аналитический слой поверх v17:

```text
исходный контент → паттерн внимания → контекст → новая оригинальная идея для клиники
```

Теперь система сохраняет не только inspiration-карточки, но и сам исходный материал, механику вовлечения и контекст, из-за которого материал может работать.

## Новые таблицы

```text
content_assets
content_patterns
content_contexts
```

## Новые команды Telegram

```text
/analyze_asset
```

Включает режим анализа материала. После команды отправьте боту:

- мем или скриншот;
- пересланный Telegram-пост;
- фото/картинку с подписью;
- текст поста;
- ссылку с пояснением.

Бот сохранит 3 слоя:

```text
1. исходный контент;
2. паттерн внимания;
3. контекст и применимость для клиники.
```

```text
/assets
```

Показать последние сохраненные исходники.

```text
/patterns
```

Показать последние найденные паттерны.

```text
/generate_from_pattern ID
```

Создать новый медицинский пост на основе выбранного паттерна, без копирования исходного материала.

## Старые команды сохранены

```text
/generate тема
/generate_full тема
/posts
/post ID
/plan_week
/plan
/create_from_plan ID
/create_full_from_plan ID
/inspire
/analyze_url ссылка
/inspirations
/plan_week_from_inspirations
```

## Railway Variables

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=production
DATABASE_URL=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_IMAGE_MODEL=gpt-image-1
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_ID=...
PUBLIC_BASE_URL=https://твой-домен.up.railway.app
TELEGRAM_PUBLISH_CHAT_ID=-100...
GENERATED_IMAGES_DIR=storage/generated_images
```

## После обновления

После замены файлов на GitHub Railway выполнит деплой. Затем снова выполните в Swagger:

```text
POST /telegram/set-webhook
```

## Проверка

1. Напишите боту:

```text
/analyze_asset
```

2. Отправьте мем, скриншот или пост.

3. Проверьте:

```text
/assets
/patterns
```

4. Создайте пост по паттерну:

```text
/generate_from_pattern 1
```
