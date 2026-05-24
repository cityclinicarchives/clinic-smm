# clinic-smm-manager v25

Версия v25 добавляет **Blueprint Layout Validator + Multi-format Layout Planner** поверх crop-and-assemble архитектуры v24.

## Главное изменение

В v24 программа уже пыталась физически вырезать элементы из исходника и собрать новую инфографику, но layout оставался слишком жестким: квадрат, 3 колонки и фиксированная высота карточек. Из-за этого часть блоков не помещалась.

В v25 pipeline такой:

```text
/analyze_asset
→ исходник сохраняется
→ /reconstruct_asset ID
→ ИИ создает structured blueprint
→ blueprint содержит canvas + blocks + layout + source_bbox
→ validator проверяет layout
→ если layout плохой, включается auto multi-format planner
→ /create_full_from_reconstruction ID
→ программа физически вырезает source_bbox из исходника
→ генерирует только недостающие/замененные элементы
→ собирает новую инфографику в подходящем формате
```

## Multi-format planner

Программа выбирает формат по сложности материала:

```text
1:1  — простая карточка / Instagram square
4:5  — основной Instagram feed
3:4  — Telegram/Pinterest инфографика
2:3  — высокая сложная инфографика
9:16 — stories/reels cover
```

Если блоков много, программа больше не пытается запихнуть всё в квадрат.

## Новые поля blueprint

Для каждого блока ИИ должен указывать не только `source_bbox`, но и `layout`:

```json
{
  "id": "mosquito",
  "type": "comparison_card",
  "title": "Комар",
  "lines": ["Зудящий волдырь", "Часто слабый зуд"],
  "visual_element": "комар + типичный след укуса",
  "source_policy": "preserve_from_reference",
  "source_bbox": {"x": 0.05, "y": 0.18, "w": 0.28, "h": 0.20},
  "layout": {"x": 0.03, "y": 0.14, "w": 0.30, "h": 0.20},
  "replacement_prompt": "",
  "change_reason": "хороший визуальный пример в исходнике"
}
```

`source_bbox` — откуда брать элемент на исходной картинке.  
`layout` — куда поставить блок на новой инфографике.

## Validator

Перед рендером программа проверяет:

```text
- есть ли карточки;
- совпадает ли expected_block_count;
- есть ли source_bbox для preserve/use_reference;
- есть ли layout;
- помещаются ли блоки в canvas;
- не пересекаются ли блоки.
```

Если AI layout плохой, программа не падает, а строит безопасный layout сама.

## Команды

```text
/analyze_asset
```
Загрузить исходник: инфографику, пост, мем, скриншот.

```text
/reconstruct_asset ID
```
Создать structured reconstruction blueprint.

```text
/create_full_from_reconstruction ID
```
Создать пост + новую инфографику по blueprint.

## Проверка после деплоя

После загрузки файлов на GitHub и деплоя Railway снова выполните:

```text
POST /telegram/set-webhook
```
