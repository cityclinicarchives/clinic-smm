POST_RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — медицинский редактор и SMM-копирайтер клиники. Реконструируй текстовые посты так, чтобы сохранять сильный hook и структуру, но улучшать экспертность, безопасность, читабельность и CTA.
Не копируй чужой текст. Верни строго валидный JSON без markdown.
""".strip()

POST_RECONSTRUCTION_USER_TEMPLATE = """
Сделай structured reconstruction blueprint для текстового/смешанного поста.

Контент-исходник #{asset_id}
Источник: {source_type}
Ссылка: {source_url}
Тип медиа: {media_type}
Текст исходника: {asset_text}
Caption: {asset_caption}
AI-анализ: {asset_analysis}
Паттерн: хук={hook_type}; эмоция={emotion}; боль={pain_point}; формат={format_type}; визуал={visual_style}; вовлечение={engagement_reason}
Контекст: {cultural_context}
Медицинская применимость: {medical_applicability}
Риски: {adaptation_risks}
Инструкция: {instruction}

Верни JSON:
{{
  "content_type": "post",
  "source_quality": "strong | medium | weak",
  "title": {{"original":"...", "evaluation":"...", "preserve_original": true, "final":"...", "change_reason":"..."}},
  "core_idea": "...",
  "pattern_summary": {{"hook":"...", "emotion":"...", "mechanic":"...", "why_it_works":"..."}},
  "medical_audit": {{"overall":"...", "correct_points":[], "corrections":[], "risk_warnings":[]}},
  "preserve": [],
  "improve": [],
  "additions": [],
  "structure": {{"kind":"post", "subtitle":"", "blocks":[], "footer":""}},
  "visual": {{"renderer_mode":"ai_image", "strategy":"какая картинка нужна к посту", "style":"...", "must_include":[], "must_avoid":[], "ai_image_prompt":"..."}},
  "post": {{"topic":"...", "text":"готовый оригинальный Telegram-пост для клиники, основанный на реконструкции"}}
}}
""".strip()
