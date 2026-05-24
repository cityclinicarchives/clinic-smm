import base64
import html
import json
import re
from typing import Any, Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset, ContentContext, ContentPattern, ContentPost, ContentReconstruction
from app.services.copywriter import generate_headline
from app.services.component_infographic_engine import generate_component_infographic_image, generate_crop_assembled_infographic_image, has_component_reference
from app.services.image_generator import generate_image_for_post
from app.services.prompt_router import select_reconstruction_prompt
from app.services.reference_image_generator import generate_reference_reconstruction_image, has_reference_image
from app.services.telegram_bot import download_file_bytes


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
Compatibility fallback. v22 uses app/prompts/* via prompt_router.
""".strip()


STRUCTURED_RECONSTRUCTION_USER_TEMPLATE = """
Compatibility fallback. v22 uses app/prompts/* via prompt_router.
""".strip()


def _format_prompt_context(
    asset: ContentAsset,
    pattern: Optional[ContentPattern],
    context: Optional[ContentContext],
    instruction: str | None,
) -> dict[str, str]:
    return {
        "asset_id": str(asset.id),
        "source_type": asset.source_type or "—",
        "source_url": asset.source_url or "—",
        "media_type": asset.media_type or "—",
        "asset_text": _cut(asset.text_content, 5000),
        "asset_caption": _cut(asset.caption, 5000),
        "asset_analysis": _cut(asset.analysis, 9000),
        "hook_type": (pattern.hook_type if pattern else "") or "",
        "emotion": (pattern.emotion if pattern else "") or "",
        "pain_point": (pattern.pain_point if pattern else "") or "",
        "format_type": (pattern.format if pattern else "") or "",
        "visual_style": (pattern.visual_style if pattern else "") or "",
        "humor_mechanic": (pattern.humor_mechanic if pattern else "") or "",
        "engagement_reason": (pattern.engagement_reason if pattern else "") or "",
        "cultural_context": (context.cultural_context if context else "") or "",
        "medical_applicability": (context.medical_applicability if context else "") or "",
        "adaptation_risks": (context.adaptation_risks if context else "") or "",
        "clinic_ideas": (context.clinic_ideas if context else "") or "",
        "instruction": instruction or "нет",
    }


def _asset_image_content(asset: ContentAsset) -> list[dict[str, Any]]:
    if not (asset.media_file_id and asset.media_type in {"photo", "document"}):
        return []
    try:
        image_bytes = download_file_bytes(asset.media_file_id)
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"}]
    except Exception:
        return []


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

    # v22: сначала выбираем специализированный prompt по типу контента.
    prompt_choice = select_reconstruction_prompt(asset, pattern)
    prompt_context = _format_prompt_context(asset, pattern, context, instruction)
    prompt = prompt_choice.user_template.format(**prompt_context)

    client = _get_client()
    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    user_content.extend(_asset_image_content(asset))

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": prompt_choice.system_prompt},
            {"role": "user", "content": user_content},
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
    spec.setdefault("router", {})
    spec["router"] = {
        "asset_type": prompt_choice.asset_type,
        "recommended_prompt": prompt_choice.recommended_prompt,
        "recommended_pipeline": prompt_choice.recommended_pipeline,
        "reason": prompt_choice.reason,
    }

    visual = spec["visual"]
    content_type = str(spec.get("content_type") or prompt_choice.asset_type or "").lower()
    kind = str(spec["structure"].get("kind") or "").lower()

    # v23: для инфографик с исходным изображением основной режим — crop_and_assemble_infographic.
    # Он заставляет image model работать не «одной большой картинкой», а по компонентам:
    # header / disclaimer / карточки / warning block / action block / footer.
    if has_reference_image(asset) and (
        content_type in {"infographic", "carousel"}
        or kind in {"infographic", "component_infographic", "carousel"}
        or str(visual.get("renderer_mode") or "").lower() == "crop_and_assemble_infographic"
    ):
        visual["renderer_mode"] = "crop_and_assemble_infographic"
    elif has_reference_image(asset) and (
        content_type in {"meme", "visual_ad"}
        or kind in {"meme", "visual_ad"}
        or prompt_choice.recommended_pipeline == "reference_based_reconstruction"
    ):
        visual["renderer_mode"] = visual.get("renderer_mode") or "reference_image_edit"
        if visual["renderer_mode"] == "deterministic_infographic":
            visual["renderer_mode"] = "reference_image_edit"
    elif not visual.get("renderer_mode"):
        visual["renderer_mode"] = "ai_image"

    # Старый deterministic_infographic оставляем только как fallback, но не используем как основной режим.
    if visual.get("renderer_mode") == "deterministic_infographic":
        visual["renderer_mode"] = "crop_and_assemble_infographic" if has_reference_image(asset) else "ai_image"

    analysis = _build_human_analysis(spec)
    title = spec.get("title") or {}
    audit = spec.get("medical_audit") or {}
    visual = spec.get("visual") or {}
    structure = spec.get("structure") or {}
    post = spec.get("post") or {}

    reconstruction = ContentReconstruction(
        asset_id=asset.id,
        content_type=str(spec.get("content_type") or prompt_choice.asset_type or "")[:255],
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
        image_prompt=str(visual.get("reference_edit_prompt") or visual.get("ai_image_prompt") or visual.get("fallback_ai_image_prompt") or "")[:8000],
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
        asset = db.query(ContentAsset).filter(ContentAsset.id == reconstruction.asset_id).first() if reconstruction.asset_id else None

        # v23: для сложных инфографик используем component-based reference reconstruction.
        # Если не сработает — fallback на обычный reference edit, затем на генерацию по blueprint.
        if mode == "crop_and_assemble_infographic" and has_component_reference(asset):
            try:
                image_path, image_prompt = generate_crop_assembled_infographic_image(
                    reconstruction=reconstruction,
                    post=post,
                    asset=asset,
                )
                post.image_model = f"{settings.openai_image_model}-component-reference"
            except Exception as component_exc:
                try:
                    image_path, image_prompt = generate_reference_reconstruction_image(
                        reconstruction=reconstruction,
                        post=post,
                        asset=asset,
                    )
                    post.image_model = f"{settings.openai_image_model}-reference-edit"
                except Exception as ref_exc:
                    custom_instruction = f"""
Component/reference reconstruction не сработал: {component_exc}; reference edit fallback: {ref_exc}

Создай изображение по COMPONENT STRUCTURED BLUEPRINT.
Работай как component-based infographic engine: сначала блоки, затем общая сборка.

Итоговый заголовок: {headline}

Blueprint JSON:
{json.dumps(spec, ensure_ascii=False)[:16000]}

Строго следуй structure.blocks, source_policy, visual.must_include и visual.must_avoid.
Для инфографик используй короткие, крупные, читаемые русские подписи.
""".strip()
                    image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=custom_instruction)
                    post.image_model = settings.openai_image_model

        elif mode == "reference_image_edit" and has_reference_image(asset):
            try:
                image_path, image_prompt = generate_reference_reconstruction_image(
                    reconstruction=reconstruction,
                    post=post,
                    asset=asset,
                )
                post.image_model = f"{settings.openai_image_model}-reference-edit"
            except Exception as exc:
                custom_instruction = f"""
Reference edit не сработал: {exc}

Создай изображение по STRUCTURED CONTENT BLUEPRINT.
Сохрани визуальную механику исходника, но не копируй watermark/бренд/username.

Итоговый заголовок: {headline}

Blueprint JSON:
{json.dumps(spec, ensure_ascii=False)[:14000]}

Строго следуй visual.must_include и visual.must_avoid.
Для инфографик используй короткие, крупные, читаемые русские подписи.
""".strip()
                image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=custom_instruction)
                post.image_model = settings.openai_image_model
        else:
            custom_instruction = f"""
Это изображение создается на основе STRUCTURED CONTENT BLUEPRINT, а не краткого пересказа.

Итоговый заголовок: {headline}

Blueprint JSON:
{json.dumps(spec, ensure_ascii=False)[:14000]}

Строго следуй visual.must_include и visual.must_avoid.
Не добавляй лишний русский текст, кроме заголовка, если это не инфографика.
Если это инфографика — делай текст коротким, крупным и проверенным по blueprint.
""".strip()
            image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=custom_instruction)
            post.image_model = settings.openai_image_model

        post.image_path = image_path
        post.image_prompt = image_prompt
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
