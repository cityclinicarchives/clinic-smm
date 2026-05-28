VISUAL_RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — арт-директор медицинского SMM. Анализируй визуальные карточки, постеры и рекламные изображения через композицию, цвет, hook, читаемость, эмоцию и применимость для клиники. Верни строго JSON.
""".strip()

VISUAL_RECONSTRUCTION_USER_TEMPLATE = """
Сделай structured reconstruction blueprint для визуального материала.

Контент-исходник #{asset_id}
Источник: {source_type}
Ссылка: {source_url}
Тип медиа: {media_type}
Текст: {asset_text}
Caption: {asset_caption}
AI-анализ: {asset_analysis}
Паттерн: хук={hook_type}; эмоция={emotion}; боль={pain_point}; формат={format_type}; визуал={visual_style}; вовлечение={engagement_reason}
Инструкция: {instruction}

Верни JSON:
{{
  "content_type": "visual_ad",
  "source_quality": "strong | medium | weak",
  "title": {{"original":"...", "evaluation":"...", "preserve_original": true, "final":"...", "change_reason":"..."}},
  "core_idea": "...",
  "pattern_summary": {{"hook":"...", "emotion":"...", "mechanic":"...", "why_it_works":"..."}},
  "medical_audit": {{"overall":"...", "correct_points":[], "corrections":[], "risk_warnings":[]}},
  "preserve": [],
  "improve": [],
  "additions": [],
  "structure": {{"kind":"visual_ad", "subtitle":"", "blocks":[], "footer":""}},
  "visual": {{"renderer_mode":"reference_image_edit", "strategy":"как улучшить визуал", "style":"...", "must_include":[], "must_avoid":[], "reference_edit_prompt":"...", "fallback_ai_image_prompt":"..."}},
  "post": {{"topic":"...", "text":"короткий пост к визуалу"}}
}}
""".strip()
