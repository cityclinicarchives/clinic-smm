import html
import json
import re
from typing import Any, Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset, ContentContext, ContentPattern, ContentPost, ContentReconstruction
from app.services.copywriter import generate_headline
from app.services.image_generator import generate_image_for_post
from app.services.infographic_renderer import render_infographic_from_reconstruction


class ReconstructionError(RuntimeError):
    pass


class ReconstructionNotFoundError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ReconstructionError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _cut(text: str | None, limit: int = 8000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...обрезано"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _field(text: str, *names: str, limit: int = 4000) -> str:
    joined = "|".join(re.escape(name) for name in names)
    pattern = rf"(?:^|\n)(?:{joined})\s*:\s*(.+?)(?=\n[A-Za-zА-Яа-яЁё0-9 _/().-]{{2,60}}\s*:|\Z)"
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()[:limit]


def get_asset_or_raise(db: Session, asset_id: int) -> ContentAsset:
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if not asset:
        raise ReconstructionError(f"Контент-исходник с ID {asset_id} не найден.")
    return asset


def _get_related_pattern_context(db: Session, asset_id: int) -> tuple[Optional[ContentPattern], Optional[ContentContext]]:
    pattern = db.query(ContentPattern).filter(ContentPattern.asset_id == asset_id).order_by(ContentPattern.id.desc()).first()
    context = db.query(ContentContext).filter(ContentContext.asset_id == asset_id).order_by(ContentContext.id.desc()).first()
    return pattern, context


STRUCTURED_RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — главный медицинский редактор, SMM-стратег, арт-директор и production-дизайнер частной медицинской клиники в Москве.

Твоя задача — создать НЕ пересказ, а STRUCTURED CONTENT BLUEPRINT: машинно-исполняемый план реконструкции контента.

Важнейшая логика:
1. Сначала оцени исходный заголовок. Если он сильный, сохрани его почти без изменений.
2. Не переписывай хороший исходник просто ради рерайта.
3. Если это инфографика, проверяй медицинскую корректность каждого пункта.
4. Если исходная инфографика в целом правильная, сохрани смысловую структуру и только улучши дизайн/читабельность.
5. Если есть ошибки — исправь.
6. Если есть важные недостающие пункты — добавь.
7. Пост и картинка должны создаваться из одного blueprint, чтобы не терять смысл реконструкции.
8. Для инфографик русский текст должен быть подготовлен как данные для рендера, а не отдан image-модели.

Медицинская безопасность:
- не ставь диагноз по картинке или симптомам;
- не обещай лечение;
- избегай категоричности "это точно дефицит";
- используй формулировки "может быть связано", "стоит обсудить с врачом", "при выраженных симптомах обратитесь за медицинской помощью".

Верни СТРОГО валидный JSON без markdown.
""".strip()


STRUCTURED_RECONSTRUCTION_USER_TEMPLATE = """
Сделай structured reconstruction blueprint для контент-исходника #{asset_id}.

Источник: {source_type}
Ссылка: {source_url}
Тип медиа: {media_type}

Текст исходника:
{asset_text}

Caption/подпись исходника:
{asset_caption}

Предыдущий AI-анализ исходника:
{asset_analysis}

Паттерн внимания:
- Хук: {hook_type}
- Эмоция: {emotion}
- Боль/желание: {pain_point}
- Формат: {format_type}
- Визуальный стиль: {visual_style}
- Юмор: {humor_mechanic}
- Почему вовлекает: {engagement_reason}

Контекст:
- Культурный/сезонный контекст: {cultural_context}
- Медицинская применимость: {medical_applicability}
- Риски адаптации: {adaptation_risks}
- Идеи для клиники: {clinic_ideas}

Дополнительная инструкция пользователя:
{instruction}

Верни JSON строго по этой схеме:
{{
  "content_type": "infographic | post | meme | video_idea | carousel | other",
  "source_quality": "strong | medium | weak",
  "title": {{
    "original": "...",
    "evaluation": "сильный/средний/слабый + почему",
    "preserve_original": true,
    "final": "...",
    "change_reason": "почему оставили или изменили"
  }},
  "core_idea": "главная идея исходника",
  "pattern_summary": {{
    "hook": "...",
    "emotion": "...",
    "mechanic": "...",
    "why_it_works": "..."
  }},
  "medical_audit": {{
    "overall": "общая оценка медицинской корректности",
    "correct_points": ["..."],
    "corrections": ["что исправить и как"],
    "risk_warnings": ["что нельзя подавать категорично"]
  }},
  "preserve": ["что обязательно сохранить"],
  "improve": ["что улучшить"],
  "additions": ["что добавить по теме, если уместно"],
  "structure": {{
    "kind": "infographic | post | meme | carousel | other",
    "subtitle": "короткий подзаголовок/обещание пользы",
    "blocks": [
      {{"title": "название блока", "lines": ["короткая строка 1", "короткая строка 2"], "note": "необязательно"}}
    ],
    "footer": "дисклеймер или CTA"
  }},
  "visual": {{
    "renderer_mode": "deterministic_infographic | ai_image",
    "strategy": "как должен выглядеть новый визуал",
    "style": "цвета, композиция, типографика",
    "must_include": ["..."],
    "must_avoid": ["..."],
    "ai_image_prompt": "если renderer_mode=ai_image, подробный промпт; если deterministic_infographic — кратко"
  }},
  "post": {{
    "topic": "тема нового поста",
    "text": "готовый текст поста для Telegram на русском языке, который использует выводы реконструкции"
  }}
}}

Для инфографики:
- renderer_mode почти всегда должен быть deterministic_infographic;
- blocks должны содержать финальные проверенные тексты для инфографики;
- не включай в blocks сомнительные утверждения;
- лучше 6-12 коротких блоков, чем длинные абзацы.
""".strip()


def _build_human_analysis(spec: dict[str, Any]) -> str:
    title = spec.get("title") or {}
    audit = spec.get("medical_audit") or {}
    visual = spec.get("visual") or {}
    structure = spec.get("structure") or {}
    pattern = spec.get("pattern_summary") or {}

    def bullets(values: Any) -> str:
        if not values:
            return "—"
        if isinstance(values, str):
            return values
        if isinstance(values, list):
            return "\n".join(f"- {v}" for v in values if str(v).strip()) or "—"
        return str(values)

    blocks = structure.get("blocks") or []
    blocks_preview = []
    if isinstance(blocks, list):
        for i, b in enumerate(blocks[:12], start=1):
            if isinstance(b, dict):
                lines = b.get("lines") or []
                if isinstance(lines, list):
                    lines_text = "; ".join(str(x) for x in lines[:3])
                else:
                    lines_text = str(lines)
                blocks_preview.append(f"{i}. {b.get('title', 'Блок')} — {lines_text}")

    return "\n".join([
        f"Тип контента: {spec.get('content_type') or '—'}",
        f"Оценка исходника: {spec.get('source_quality') or '—'}",
        "",
        f"Исходный заголовок: {title.get('original') or '—'}",
        f"Итоговый заголовок: {title.get('final') or '—'}",
        f"Оценка заголовка: {title.get('evaluation') or '—'}",
        f"Причина изменения: {title.get('change_reason') or '—'}",
        "",
        f"Идея: {spec.get('core_idea') or '—'}",
        f"Хук: {pattern.get('hook') or '—'}",
        f"Эмоция: {pattern.get('emotion') or '—'}",
        f"Механика: {pattern.get('mechanic') or '—'}",
        f"Почему работает: {pattern.get('why_it_works') or '—'}",
        "",
        f"Медицинский аудит: {audit.get('overall') or '—'}",
        "Что исправляем:",
        bullets(audit.get('corrections')),
        "",
        "Что добавляем:",
        bullets(spec.get('additions')),
        "",
        "Структура визуала:",
        "\n".join(blocks_preview) or "—",
        "",
        f"Визуальная стратегия: {visual.get('strategy') or '—'}",
        f"Renderer mode: {visual.get('renderer_mode') or '—'}",
    ])


def _spec_text(spec: dict[str, Any], *path: str, default: str = "") -> str:
    current: Any = spec
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    if current is None:
        return default
    return str(current)


def _spec_list_text(spec: dict[str, Any], key: str) -> str:
    values = spec.get(key)
    if isinstance(values, list):
        return "\n".join(f"- {v}" for v in values if str(v).strip())
    return str(values or "")


def _fallback_spec_from_text(text: str) -> dict[str, Any]:
    return {
        "content_type": _field(text, "Тип контента", limit=255) or "other",
        "source_quality": "medium",
        "title": {
            "original": _field(text, "Исходный заголовок", limit=500),
            "evaluation": _field(text, "Оценка заголовка", limit=1000),
            "preserve_original": False,
            "final": _field(text, "Итоговый заголовок", limit=500) or "Полезная памятка",
            "change_reason": "fallback from legacy text reconstruction",
        },
        "core_idea": "Реконструкция исходного материала",
        "medical_audit": {"overall": _field(text, "Медицинский аудит", limit=4000), "corrections": [], "risk_warnings": []},
        "preserve": [_field(text, "Что сохраняем", limit=2000)],
        "improve": [_field(text, "Что исправляем", limit=2500)],
        "additions": [_field(text, "Добавления по теме", limit=3000)],
        "structure": {
            "kind": "infographic" if "инфограф" in text.lower() else "post",
            "subtitle": "Коротко и понятно",
            "blocks": [],
            "footer": "Информация носит справочный характер и не заменяет консультацию врача.",
        },
        "visual": {"renderer_mode": "ai_image", "strategy": _field(text, "Стратегия визуала", limit=3000), "ai_image_prompt": _field(text, "Промпт для изображения", limit=6000)},
        "post": {"topic": _field(text, "Тема поста", limit=500), "text": _field(text, "Текст поста", limit=8000)},
    }


def reconstruct_asset_with_ai(db: Session, asset_id: int, instruction: str | None = None) -> ContentReconstruction:
    asset = get_asset_or_raise(db, asset_id)
    pattern, context = _get_related_pattern_context(db, asset.id)

    prompt = STRUCTURED_RECONSTRUCTION_USER_TEMPLATE.format(
        asset_id=asset.id,
        source_type=asset.source_type or "—",
        source_url=asset.source_url or "—",
        media_type=asset.media_type or "—",
        asset_text=_cut(asset.text_content, 5000),
        asset_caption=_cut(asset.caption, 5000),
        asset_analysis=_cut(asset.analysis, 8000),
        hook_type=(pattern.hook_type if pattern else "") or "",
        emotion=(pattern.emotion if pattern else "") or "",
        pain_point=(pattern.pain_point if pattern else "") or "",
        format_type=(pattern.format if pattern else "") or "",
        visual_style=(pattern.visual_style if pattern else "") or "",
        humor_mechanic=(pattern.humor_mechanic if pattern else "") or "",
        engagement_reason=(pattern.engagement_reason if pattern else "") or "",
        cultural_context=(context.cultural_context if context else "") or "",
        medical_applicability=(context.medical_applicability if context else "") or "",
        adaptation_risks=(context.adaptation_risks if context else "") or "",
        clinic_ideas=(context.clinic_ideas if context else "") or "",
        instruction=instruction or "нет",
    )

    client = _get_client()
    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": STRUCTURED_RECONSTRUCTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.output_text.strip()
    try:
        spec = _extract_json(raw)
    except Exception:
        spec = _fallback_spec_from_text(raw)

    # Safety defaults.
    spec.setdefault("title", {})
    spec.setdefault("structure", {})
    spec.setdefault("visual", {})
    spec.setdefault("post", {})
    if not spec["visual"].get("renderer_mode"):
        if str(spec.get("content_type", "")).lower().find("infographic") >= 0 or str(spec["structure"].get("kind", "")).lower() == "infographic":
            spec["visual"]["renderer_mode"] = "deterministic_infographic"
        else:
            spec["visual"]["renderer_mode"] = "ai_image"

    analysis = _build_human_analysis(spec)
    title = spec.get("title") or {}
    audit = spec.get("medical_audit") or {}
    visual = spec.get("visual") or {}
    structure = spec.get("structure") or {}
    post = spec.get("post") or {}

    reconstruction = ContentReconstruction(
        asset_id=asset.id,
        content_type=str(spec.get("content_type") or "")[:255],
        original_title=str(title.get("original") or "")[:1000],
        final_title=str(title.get("final") or "")[:1000],
        title_evaluation=str(title.get("evaluation") or "")[:2000],
        preserved_elements=_spec_list_text(spec, "preserve")[:4000],
        corrected_elements=("\n".join(str(x) for x in (audit.get("corrections") or [])) if isinstance(audit.get("corrections"), list) else str(audit.get("corrections") or ""))[:5000],
        medical_audit=json.dumps(audit, ensure_ascii=False)[:8000],
        additions=_spec_list_text(spec, "additions")[:5000],
        infographic_structure=json.dumps(structure, ensure_ascii=False)[:10000],
        visual_strategy=str(visual.get("strategy") or "")[:5000],
        post_topic=str(post.get("topic") or title.get("final") or "Пост на основе реконструкции")[:500],
        post_text=str(post.get("text") or "")[:12000],
        image_prompt=str(visual.get("ai_image_prompt") or "")[:8000],
        reconstruction_spec=json.dumps(spec, ensure_ascii=False, indent=2),
        analysis=analysis,
    )
    db.add(reconstruction)
    db.commit()
    db.refresh(reconstruction)
    return reconstruction


def _load_spec(reconstruction: ContentReconstruction) -> dict[str, Any]:
    if reconstruction.reconstruction_spec:
        try:
            return json.loads(reconstruction.reconstruction_spec)
        except Exception:
            pass
    return _fallback_spec_from_text(reconstruction.analysis or "")


def create_post_from_reconstruction(db: Session, reconstruction_id: int, with_image: bool = True) -> ContentPost:
    reconstruction = db.query(ContentReconstruction).filter(ContentReconstruction.id == reconstruction_id).first()
    if not reconstruction:
        raise ReconstructionNotFoundError(f"Реконструкция с ID {reconstruction_id} не найдена.")

    spec = _load_spec(reconstruction)
    topic = (_spec_text(spec, "post", "topic") or reconstruction.post_topic or reconstruction.final_title or "Пост на основе реконструкции")[:255]
    text = _spec_text(spec, "post", "text") or reconstruction.post_text or reconstruction.analysis or ""
    headline = (_spec_text(spec, "title", "final") or reconstruction.final_title or generate_headline(topic=topic, text=text))[:255]

    post = ContentPost(
        title=topic,
        headline=headline,
        platform="telegram",
        text=text,
        status="generated",
        ai_model=settings.openai_model,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    reconstruction.created_post_id = post.id
    db.commit()
    db.refresh(reconstruction)

    if with_image:
        visual = spec.get("visual") or {}
        mode = str(visual.get("renderer_mode") or "").lower()
        kind = str((spec.get("structure") or {}).get("kind") or spec.get("content_type") or "").lower()

        if mode == "deterministic_infographic" or kind == "infographic":
            image_path, image_prompt = render_infographic_from_reconstruction(reconstruction)
        else:
            custom_instruction = f"""
Это изображение создается на основе STRUCTURED CONTENT BLUEPRINT, а не краткого пересказа.

Итоговый заголовок: {headline}

Blueprint JSON:
{json.dumps(spec, ensure_ascii=False)[:12000]}

Строго следуй visual.must_include и visual.must_avoid.
Не добавляй лишний русский текст, кроме заголовка.
""".strip()
            image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=custom_instruction)

        post.image_path = image_path
        post.image_prompt = image_prompt
        post.image_model = settings.openai_image_model if mode != "deterministic_infographic" else "pillow-deterministic-v21"
        db.commit()
        db.refresh(post)

    return post


def list_reconstructions(db: Session, limit: int = 10) -> list[ContentReconstruction]:
    return db.query(ContentReconstruction).order_by(ContentReconstruction.id.desc()).limit(limit).all()


def format_reconstruction_card(item: ContentReconstruction) -> str:
    spec = _load_spec(item)
    visual = spec.get("visual") or {}
    audit = spec.get("medical_audit") or {}
    additions = spec.get("additions") or []
    if isinstance(additions, list):
        additions_text = "\n".join(f"- {x}" for x in additions[:6]) or "—"
    else:
        additions_text = str(additions or "—")

    corrections = audit.get("corrections") if isinstance(audit, dict) else None
    if isinstance(corrections, list):
        corrections_text = "\n".join(f"- {x}" for x in corrections[:6]) or "—"
    else:
        corrections_text = str(corrections or item.corrected_elements or "—")

    return "\n".join([
        f"<b>Реконструкция #{item.id}</b>",
        f"Исходник: #{item.asset_id}",
        f"Тип: {html.escape(item.content_type or '—')}",
        f"Пост: #{item.created_post_id}" if item.created_post_id else "Пост: еще не создан",
        "",
        f"<b>Итоговый заголовок:</b> {html.escape(item.final_title or '—')}",
        f"<b>Оценка заголовка:</b> {html.escape(_cut(item.title_evaluation, 500) or '—')}",
        "",
        f"<b>Что исправляем:</b> {html.escape(_cut(corrections_text, 900) or '—')}",
        f"<b>Добавления:</b> {html.escape(_cut(additions_text, 900) or '—')}",
        "",
        f"<b>Визуальная стратегия:</b> {html.escape(_cut(item.visual_strategy, 900) or '—')}",
        f"<b>Рендер:</b> {html.escape(str(visual.get('renderer_mode') or '—'))}",
    ])
