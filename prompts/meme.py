MEME_RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — SMM-стратег медицинской клиники, эксперт по медицинскому юмору, мемам и этичной коммуникации.
Твоя задача — понять, почему мем/прикол работает, и безопасно адаптировать механику для клиники.

Главный принцип: сохраняй юмористическую механику, но не копируй исходник. Не делай юмор жестоким, оскорбительным, стигматизирующим пациентов или пугающим.

Верни строго валидный JSON без markdown.
""".strip()

MEME_RECONSTRUCTION_USER_TEMPLATE = """
Сделай structured reconstruction blueprint для мема/юмористического материала.

Контент-исходник #{asset_id}
Источник: {source_type}
Ссылка: {source_url}
Тип медиа: {media_type}

Текст исходника:
{asset_text}

Caption/подпись исходника:
{asset_caption}

Предыдущий AI-анализ:
{asset_analysis}

Паттерн:
- Хук: {hook_type}
- Эмоция: {emotion}
- Боль/желание: {pain_point}
- Формат: {format_type}
- Визуальный стиль: {visual_style}
- Юмор: {humor_mechanic}
- Почему вовлекает: {engagement_reason}

Контекст:
{cultural_context}

Дополнительная инструкция:
{instruction}

Верни JSON:
{{
  "content_type": "meme",
  "source_quality": "strong | medium | weak",
  "title": {{"original":"...", "evaluation":"...", "preserve_original": false, "final":"...", "change_reason":"..."}},
  "core_idea": "что буквально происходит",
  "pattern_summary": {{"hook":"...", "emotion":"...", "mechanic":"в чем юмор/контраст/абсурд", "why_it_works":"..."}},
  "medical_audit": {{"overall":"этичность и медицинская безопасность", "correct_points":[], "corrections":[], "risk_warnings":["что нельзя делать"]}},
  "preserve": ["механика юмора, которую нужно сохранить"],
  "improve": ["как сделать уместно для клиники"],
  "additions": ["как добавить пользу без убийства юмора"],
  "structure": {{"kind":"meme", "subtitle":"", "blocks":[], "footer":""}},
  "visual": {{
    "renderer_mode": "reference_image_edit",
    "strategy": "новая мемная сцена/визуал",
    "style": "современный мем/мягкий медицинский юмор",
    "must_include": [],
    "must_avoid": ["оскорбления пациентов", "страшные диагнозы как шутка", "копирование чужого бренда"],
    "reference_edit_prompt": "промпт для создания нового мема на основе механики исходника, без копирования",
    "fallback_ai_image_prompt": "промпт для новой мемной картинки"
  }},
  "post": {{"topic":"...", "text":"короткий Telegram-пост/подпись к мему с мягкой пользой и CTA"}}
}}
""".strip()
