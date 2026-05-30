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

## v41.2 changes: editorial replacement review + source-preserving PNG extraction

This build adds two stabilization changes:

1. Semantic analysis now asks the model to run a `replacement_review` before removing or merging a comparison-card entity. The model must consider regional, thematic, and medical analogs first. Removal is allowed, but only after explaining why replacement is not suitable.
2. Semantic PNG tasks now include `quality_strategy` and `source_crop_hint.relative_box`. For `extract_from_source` tasks, the generator first tries to crop directly from the original Telegram image and saves the crop at native source resolution without AI regeneration/upscaling. AI image generation is used for `generate_new`, `regenerate_high_detail`, `redraw_from_reference`, or when a usable crop hint is unavailable.


## v41.3 cost tracking

This build adds estimated cost tracking for the v41 pipeline.

Telegram messages now show an approximate cost block after:

- `/analyze_source` / v41 semantic analysis button
- `/generate_semantic_png 23`

The estimate includes:

- input/output tokens for text model calls when OpenAI returns `usage`;
- number of image generations for Semantic PNG;
- estimated USD cost;
- a warning if the model is unknown in the local price table.

Cost logs are also appended to:

```text
storage/costs/cost-log.jsonl
```

The JSON analysis includes:

```text
payload.analysis_state.cost_estimate
payload.custom.cost_estimate
```

The Semantic PNG manifest includes:

```text
storage/semantic_png/asset-*/state-*/manifest.json
```

with `cost_estimate`.

Pricing can be overridden in Railway Variables:

```text
COST_TRACKING_ENABLED=true
COST_TEXT_INPUT_USD_PER_1M=0
COST_TEXT_OUTPUT_USD_PER_1M=0
COST_IMAGE_1024_USD=0
```

Leave values as `0` to use the built-in default price table. The estimate is not a billing document; actual OpenAI charges can differ because of current pricing, image quality/size, prompt caching, retries, and account settings.

## v41.2 Semantic PNG extraction fix

This build improves the Semantic PNG step:

- `semantic_png_plan` prompt now requires safe crop boxes with a small internal margin and explicit exclusion of old labels/UI.
- `extract_from_source` now adds a modest safe crop margin to avoid cutting circle borders, insect legs/wings, and outlines.
- Background cleanup is now border-flood based: it removes only the page background connected to crop edges and preserves skin gradients.
- A text-fragment cleanup pass removes small disconnected dark components near crop edges to reduce leftover English labels.
- Extracted PNGs are saved at native crop resolution; no artificial upscale is applied.
- Generated replacement PNG prompts now use a stronger style lock: same round skin visual, contour, palette, scale, and illustration density as preserved source cards.

## v41 update: edge-aware semantic PNG extraction

This version improves `extract_from_source` PNG extraction without extra AI image calls:

- wide crop is treated only as a search area;
- border-connected background is removed by edge-aware flood fill;
- extraction no longer assumes circular objects;
- disconnected text-like fragments near crop edges are removed;
- object is trimmed to its visible foreground bbox and padded on a transparent square canvas;
- no artificial upscaling is applied to source crops.

Expected effect: fewer cut contours, fewer leftover labels, less old beige/white background, more stable semantic PNGs across round medical visuals and irregular foreground objects.

## v41.1 Persistent semantic analysis storage

Дорогой этап `semantic_analysis` теперь сохраняется не только в `storage/analysis`, но и в PostgreSQL в таблицу `semantic_analyses`.

Зачем это нужно:
- Railway может очищать локальные файлы после redeploy/restart.
- Повторный v41-анализ стоит денег.
- Команды `/generate_semantic_png <asset_id>` и `/compose_reconstruction <asset_id>` должны переиспользовать уже сохраненный анализ.

Поведение команд:
- `/list_analysis` сначала показывает анализы из PostgreSQL.
- `/get_analysis 23` выгружает JSON из PostgreSQL во временный файл и отправляет его в Telegram.
- `/generate_semantic_png 23` сначала читает анализ из PostgreSQL, и только если его нет — ищет старый файл в `storage/analysis`.

Новая таблица создается автоматически при старте приложения через `Base.metadata.create_all()`. Мини-миграция также добавляет недостающие поля через `ensure_database_schema()`.
