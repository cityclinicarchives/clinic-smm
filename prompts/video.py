VIDEO_RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — сценарист Shorts/Reels для медицинской клиники. Анализируй короткие видео через hook первых 2 секунд, удержание, монтаж, конфликт, пользу и CTA. Верни строго валидный JSON без markdown.
""".strip()

VIDEO_RECONSTRUCTION_USER_TEMPLATE = """
Сделай structured reconstruction blueprint для короткого видео/идеи видео.

Контент-исходник #{asset_id}
Источник: {source_type}
Ссылка: {source_url}
Тип медиа: {media_type}
Текст/описание: {asset_text}
Caption: {asset_caption}
AI-анализ: {asset_analysis}
Паттерн: хук={hook_type}; эмоция={emotion}; боль={pain_point}; формат={format_type}; визуал={visual_style}; вовлечение={engagement_reason}
Инструкция: {instruction}

Верни JSON:
{{
  "content_type": "video_idea",
  "source_quality": "strong | medium | weak",
  "title": {{"original":"...", "evaluation":"...", "preserve_original": false, "final":"...", "change_reason":"..."}},
  "core_idea": "...",
  "pattern_summary": {{"hook":"первые 2 секунды", "emotion":"...", "mechanic":"...", "why_it_works":"..."}},
  "medical_audit": {{"overall":"...", "correct_points":[], "corrections":[], "risk_warnings":[]}},
  "preserve": [],
  "improve": [],
  "additions": [],
  "structure": {{"kind":"video_idea", "subtitle":"", "blocks":[{{"title":"Кадр 1", "lines":["..."]}}], "footer":"CTA"}},
  "visual": {{"renderer_mode":"ai_image", "strategy":"обложка/кадр для видео", "style":"...", "must_include":[], "must_avoid":[], "ai_image_prompt":"..."}},
  "post": {{"topic":"...", "text":"пост-анонс или сценарий для ролика"}}
}}
""".strip()
