ROUTER_SYSTEM_PROMPT = """
Ты — router-классификатор контента для AI-SMM системы медицинской клиники.
Определи тип материала и рекомендуемый pipeline. Верни строго JSON без markdown.
""".strip()

ROUTER_USER_TEMPLATE = """
Определи тип контента.

Источник: {source_type}
Тип медиа: {media_type}
Текст: {text}
Caption: {caption}
Предыдущий анализ: {analysis}

Верни JSON:
{{
  "asset_type": "infographic | meme | post | short_video | visual_ad | carousel | other",
  "content_mode": "educational_medical | humor | promotional | mixed | other",
  "humor_level": "none | soft | strong",
  "needs_medical_audit": true,
  "needs_reference_image": true,
  "recommended_prompt": "infographic | meme | post | video | visual | generic",
  "recommended_pipeline": "reference_based_reconstruction | text_reconstruction | video_script_reconstruction | generic_reconstruction",
  "reason": "почему выбран этот тип"
}}
""".strip()
