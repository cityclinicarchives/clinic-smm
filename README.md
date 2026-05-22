# clinic-smm-manager v22

AI-SMM backend for a medical clinic.

## Что нового в v22

Добавлена новая архитектура промптов:

- `app/prompts/router.py` — определяет тип контента.
- `app/prompts/infographic.py` — отдельный промпт для инфографик.
- `app/prompts/meme.py` — отдельный промпт для мемов и медицинского юмора.
- `app/prompts/post.py` — отдельный промпт для текстовых постов.
- `app/prompts/video.py` — отдельный промпт для Shorts/Reels/video ideas.
- `app/prompts/visual.py` — отдельный промпт для визуальных карточек и постеров.
- `app/services/prompt_router.py` — выбирает правильный промпт.
- `app/services/reference_image_generator.py` — пытается генерировать новую картинку на основе исходной картинки-референса.

Главное изменение: для инфографик, мемов и визуальных карточек программа больше не должна создавать изображение с нуля, если есть исходная картинка. Она использует reference-based reconstruction:

```text
исходная картинка
+ специализированный промпт
+ structured reconstruction spec
→ новая улучшенная картинка
```

## Основной workflow

```text
/analyze_asset
```

Отправьте боту исходный материал: инфографику, пост, мем, скриншот, фото или ссылку.

```text
/reconstruct_asset ID
```

Бот определит тип контента, выберет специализированный промпт и создаст structured reconstruction blueprint.

```text
/create_full_from_reconstruction ID
```

Бот создаст пост и изображение на основе reconstruction spec.

## Важно

Для инфографик и мемов лучше загружать именно картинку, а не только текст. Тогда v22 сможет использовать исходник как визуальный референс.

После деплоя снова выполните:

```text
POST /telegram/set-webhook
```
