# clinic-smm-manager — semantic reconstruction baseline step 1

Эта версия добавляет первый этап новой дешевой архитектуры реконструкции инфографик:

```text
Upload infographic
→ one multimodal semantic analysis call
→ Visual Entity Map
→ Semantic PNG Plan
→ Design Blueprint
→ Post draft
→ QA checklist
```

## Новый endpoint

```text
POST /assets/{asset_id}/semantic-reconstruction/analyze
```

Он создает `ProjectState` со следующими разделами:

```text
analysis_state
visual_entity_map
semantic_png_plan
design_blueprint
post
qa_checklist
continuation_package
```

## Что важно

- Никаких hardcoded правил под конкретную инфографику.
- Регион: Россия / Москва / Средняя полоса России.
- На этом шаге программа НЕ генерирует финальную картинку.
- Она только создает смысловую карту и план для последующих дешевых этапов.

## Рекомендуемая модель

Для первого мультимодального аналитического этапа выбрана модель:

```text
OPENAI_MODEL=gpt-5.5
```

Причина: этот этап самый важный для качества всей реконструкции. Он должен одновременно понимать изображение, медицинский смысл, визуальную структуру, региональную уместность и возвращать строгий JSON-план. Для экономии на следующих этапах лучше сделать один качественный аналитический вызов, чем несколько слабых повторных вызовов.

На Railway установите переменную:

```text
OPENAI_MODEL=gpt-5.5
```


## v41 step1 Telegram test patch

Telegram now starts only the first stage of the new architecture for uploaded infographic assets.

Button:

```text
🧠 Анализировать инфографику (v41)
```

It runs semantic analysis equivalent to:

```text
POST /assets/{asset_id}/semantic-reconstruction/analyze
```

The full JSON analysis is saved to:

```text
storage/analysis/
```

This stage is intentionally limited to analysis only: visual entities, semantic PNG plan, design blueprint, post draft and QA checklist.

## v41.1 compact semantic reconstruction

Commands in Telegram:

- `/list_analysis` — list saved semantic-analysis JSON files.
- `/get_analysis 23` — download compact v41.1 JSON for asset 23.
- `/generate_semantic_png 23` — generate semantic PNG assets from `semantic_png_plan`.
- `/compose_reconstruction 23` — compose final 1080x1350 infographic from `design_blueprint`, `content_pack`, and saved semantic PNG assets.

Saved files:

- `storage/analysis/asset-*-state-*-semantic-analysis.json`
- `storage/semantic_png/asset-*/state-*/*.png`
- `storage/reconstructions/asset-*-state-*-reconstruction.png`

Important: these files are stored inside Railway filesystem unless you connect a Railway Volume or external storage.
