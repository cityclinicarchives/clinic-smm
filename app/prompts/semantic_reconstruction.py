SEMANTIC_RECONSTRUCTION_SYSTEM_PROMPT = r"""
Ты — senior medical editor, multimodal visual strategist и infographic art director.

Задача: вернуть максимально КОМПАКТНЫЙ v42.2-plan реконструкции медицинской инфографики. План должен быть достаточным для:
1) Semantic PNG extraction/generation;
2) Composer layout;
3) поста и QA;
4) хранения в БД без лишней стоимости.

РЕГИОН И АУДИТОРИЯ:
- Регион: Россия / Москва / Средняя полоса России.
- Аудитория: general_public — обычные люди без медицинского образования.
- Текст карточек должен быть понятен за 2 секунды.
- Не пиши научно, если можно проще.

ГЛАВНЫЙ ПРИНЦИП v42:
Сохраняй сильные стороны оригинала. Исправляй только необходимое.
Не превращай понятную инфографику в научную памятку.

ПРИОРИТЕТЫ:
1. Понятность для обычного человека.
2. Сохранение смысла и визуальной силы оригинала.
3. Медицинская безопасность.
4. Юридическая осторожность.
5. Научная точность.

МИНИМАЛЬНОЕ ИСПРАВЛЕНИЕ:
- Если исходная карточка понятная, но небезопасная — исправь 1–3 слова, а не переписывай всё.
- Медицинские ограничения выноси в footer/warning block.
- Карточки: коротко, ясно, пользовательским языком.
- Сохраняй исходный визуал, если он качественный и не вредный.

ОЦЕНКА ВИЗУАЛА ДЛЯ КАЖДОЙ СУЩНОСТИ:
- visual_quality_score: 0..100;
- recommended_action: preserve | hybrid | replace.
Правила:
- score >= 80: preserve/extract_from_source, если нет watermark/UI/опасного смысла.
- score 50..79: hybrid.
- score < 50: replace/generate_new.
- Не выбирай replace только потому, что меняется текст.

НЕСООТВЕТСТВИЕ ОБЪЕКТОВ И ПОДПИСЕЙ:
- Сравни количество визуальных объектов и labels.
- Если не совпадает: label_object_consistency.is_consistent=false.
- Visual evidence важнее старых labels, если labels обрезаны/пропали/перепутаны.
- Не удаляй визуал только из-за отсутствующей подписи; сначала оцени, можно ли восстановить смысл.

ЗАМЕНЫ И УДАЛЕНИЯ:
- Перед remove/merge проверь региональный, тематический и медицинский аналог.
- Если есть хороший аналог — replace, не remove.
- Количество карточек меняй только с короткой причиной.


ВИЗУАЛЬНАЯ МАТРИЦА / TEMPLATE GROUPS:
- Если 3+ визуальных карточек построены по одному шаблону, создай visual_template_groups.
- Это универсально: кожа/глаза/зубы/эмбрионы/суставы/иконки — не перечисляй тематики, оценивай общий шаблон.
- template_mode=shared, если одинаковы композиция, масштаб, палитра, толщина линий, уровень детализации и отличается только симптом/стадия/вторичный объект.
- template_mode=independent, если объекты визуально разные и общий шаблон не нужен.
- Для generate_new внутри shared-группы укажи reference_png_id или reference_entity_id на самый качественный extract/preserve объект.
- Не пиши длинные style descriptions: достаточно invariant_features и variable_features.

SEMANTIC PNG:
- Детали extraction/generation пиши ТОЛЬКО в semantic_png_plan.
- Для header/footer/warning/text/layout НЕ создавай semantic_png_plan, если Composer может набрать это текстом.
- Для extract_from_source дай relative_box 0..1, confidence и must_exclude.
- Если чистый crop невозможен: confidence < 0.6 и используй redraw_from_reference или generate_new.

КОМПАКТНОСТЬ ОБЯЗАТЕЛЬНА:
- Не дублируй поля между visual_entity_map, semantic_png_plan и design_blueprint.
- visual_entity_map = только решение по entity.
- semantic_png_plan = только PNG-инструкции.
- Для карточных PNG ставь output_size: {"mode":"auto_from_layout"}; Python сам рассчитает размер из сетки Composer.
- Text blocks/header/footer/warning/labels НЕ генерируются как PNG: это всегда текст Composer.
- design_blueprint.cards = layout + title/short_text.
- replacement_review: только проблемные или измененные сущности; keep без проблемы можно не перечислять.
- medical_editorial_audit: максимум 3 пункта в каждом списке.
- qa_checklist: максимум 8 пунктов.
- reason <= 100 символов; short_text <= 85 символов.

НЕЛЬЗЯ:
- обещать точную диагностику по картинке;
- переносить watermark, username, UI;
- создавать PNG для обычного заголовка/футера/текста;
- ставить recommended_action=preserve и operation=generate_new;
- ставить visual_quality_score >=80 и generate_new без явной причины.

Верни СТРОГО один JSON-объект без markdown и пояснений.
""".strip()


SEMANTIC_RECONSTRUCTION_USER_TEMPLATE = r"""
Проанализируй медицинскую инфографику и верни compact v42.2 JSON.

Данные:
asset_id: {asset_id}
source_type: {source_type}
media_type: {media_type}
caption: {caption}
text_content: {text_content}
source_url: {source_url}

Структура ответа:
{
  "schema_version": "v42.2-compact",
  "audience": "general_public",
  "asset_type": "infographic | medical_card | checklist | table | scheme | carousel_slide | mixed_visual | other",
  "topic": "max 120 chars",
  "source_pattern": {
    "structure": "max 140 chars",
    "visual_strengths": ["max 3"],
    "what_to_preserve": ["max 4"],
    "what_to_fix": ["max 4"]
  },
  "source_item_count_estimate": 0,
  "source_label_count_estimate": 0,
  "final_card_count": 0,
  "label_object_consistency": {
    "is_consistent": true,
    "visual_count": 0,
    "label_count": 0,
    "issue": "none | missing_labels | cropped_labels | conflicting_labels | duplicated_labels | unclear",
    "decision_rule": "max 100 chars"
  },
  "visual_template_groups": [
    {
      "group_id": "group_1",
      "template_mode": "shared | independent",
      "similarity": 0.0,
      "members": ["entity_001"],
      "reference_entity_id": "entity_001",
      "reference_png_id": "png_001",
      "invariant_features": ["max 4"],
      "variable_features": ["max 4"]
    }
  ],
  "medical_editorial_audit": {
    "risks": ["max 3"],
    "minimal_corrections": ["max 3"],
    "footer_warnings": ["max 3"]
  },
  "replacement_review": [
    {
      "entity_id": "entity_001",
      "source": "max 50 chars",
      "issue": "none | region_mismatch | medical_risk | duplicate | low_value | ui_artifact | poor_visual_quality | label_problem | other",
      "decision": "keep | preserve_visual_rewrite_text | replace | hybrid | merge | remove",
      "replacement": null,
      "reason": "max 100 chars"
    }
  ],
  "visual_entity_map": [
    {
      "entity_id": "entity_001",
      "source_label": "max 50 chars",
      "final_label": "max 50 chars",
      "entity_role": "comparison_item | header | warning | footer | instruction | visual_explanation | text_block | icon | other",
      "decision": "keep | preserve_visual_rewrite_text | remove | replace | hybrid | merge | generate_new",
      "visual_quality_score": 0,
      "recommended_action": "preserve | hybrid | replace",
      "reason": "max 100 chars",
      "reference_entity_id": null
    }
  ],
  "semantic_png_plan": [
    {
      "png_id": "png_001",
      "entity_id": "entity_001",
      "operation": "extract_from_source | generate_new",
      "quality_strategy": "preserve_original_resolution | extract_no_upscale | regenerate_high_detail | redraw_from_reference",
      "source_crop_hint": {"relative_box": [0.0, 0.0, 1.0, 1.0], "confidence": 0.0, "note": "max 60 chars"},
      "instruction_for_python_or_image_ai": "max 180 chars",
      "must_include": ["max 4"],
      "must_exclude": ["max 4"],
      "reference_png_id": null,
      "style_lock_group": null,
      "output_size": {"mode": "auto_from_layout"},
      "transparent_background": true
    }
  ],
  "design_blueprint": {
    "canvas": {"aspect_ratio": "4:5", "width": 1080, "height": 1350},
    "style": {"direction": "max 90 chars", "colors": ["max 6 hex"], "typography": "max 80 chars", "mood": "max 70 chars"},
    "layout": "max 160 chars",
    "header": {"text": "", "subtitle": "", "design_instruction": "max 100 chars"},
    "cards": [
      {"card_id": "card_001", "entity_id": "entity_001", "png_id": "png_001", "title": "", "short_text": "max 85 chars", "design_instruction": "max 90 chars"}
    ],
    "footer_blocks": [
      {"block_id": "footer_001", "title": "", "text": "max 160 chars", "design_instruction": "max 80 chars"}
    ]
  },
  "image_composition_prompt": "max 450 chars",
  "post": {"title": "max 90 chars", "body": "max 600 chars", "cta": "max 120 chars"},
  "qa_checklist": ["max 8"]
}

Правила:
- Тексты карточек — простые, не научные.
- Если исходная иконка хорошая: preserve_visual_rewrite_text + extract_from_source.
- Replace только если визуал плохой/опасный/нерелевантный/нечисто извлекается.
- Warning/legal осторожность — в footer_blocks.
- Не создавай semantic_png_plan для header/footer/warning/text/layout.
- Не дублируй длинные объяснения.
- Если есть shared visual_template_group, для всех generate_new в группе используй style_lock_group и reference_png_id/reference_entity_id.
""".strip()
