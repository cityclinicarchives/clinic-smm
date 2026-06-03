import base64
import html
import json
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset, ContentContext, ContentPattern, ContentPost
from app.services.copywriter import generate_headline
from app.services.image_generator import generate_image_for_post
from app.services.telegram_bot import download_file_bytes


class AssetAnalyzeError(RuntimeError):
    pass


class AssetNotFoundError(RuntimeError):
    pass


class PatternNotFoundError(RuntimeError):
    pass


@dataclass
class AssetInput:
    source_type: str
    source_url: str | None = None
    text: str | None = None
    caption: str | None = None
    media_type: str | None = None
    media_file_id: str | None = None
    media_bytes: bytes | None = None
    media_mime: str | None = None
    raw_meta: str | None = None


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise AssetAnalyzeError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _cut(text: str | None, limit: int = 7000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...обрезано"


def _field(text: str, *names: str, limit: int = 2000) -> str:
    joined = "|".join(re.escape(name) for name in names)
    pattern = rf"(?:^|\n)(?:{joined})\s*:\s*(.+?)(?=\n[A-Za-zА-Яа-яЁё _-]{{2,40}}\s*:|\Z)"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()[:limit]


def build_asset_input_from_telegram_message(message: dict) -> AssetInput:
    text = message.get("text") or ""
    caption = message.get("caption") or ""
    combined = f"{text}\n{caption}"
    source_url = None
    url_match = re.search(r"https?://\S+", combined)
    if url_match:
        source_url = url_match.group(0).rstrip(".,);]")

    media_type = None
    media_file_id = None
    media_bytes = None
    media_mime = None

    if message.get("photo"):
        media_type = "photo"
        largest = message["photo"][-1]
        media_file_id = largest.get("file_id")
        if media_file_id:
            try:
                media_bytes = download_file_bytes(media_file_id)
                media_mime = "image/jpeg"
            except Exception:
                media_bytes = None
                media_mime = None
    elif message.get("video"):
        media_type = "video"
        media_file_id = message["video"].get("file_id")
        media_mime = message["video"].get("mime_type") or "video/mp4"
    elif message.get("document"):
        media_type = "document"
        media_file_id = message["document"].get("file_id")
        media_mime = message["document"].get("mime_type")
        if media_file_id and media_mime and media_mime.startswith("image/"):
            try:
                media_bytes = download_file_bytes(media_file_id)
            except Exception:
                media_bytes = None

    raw_meta_lines: list[str] = []
    if message.get("forward_origin"):
        raw_meta_lines.append(f"forward_origin: {message.get('forward_origin')}")
    if message.get("forward_from_chat"):
        raw_meta_lines.append(f"forward_from_chat: {message.get('forward_from_chat')}")
    if message.get("date"):
        raw_meta_lines.append(f"telegram_date: {message.get('date')}")

    return AssetInput(
        source_type="telegram_forward_or_manual",
        source_url=source_url,
        text=text,
        caption=caption,
        media_type=media_type,
        media_file_id=media_file_id,
        media_bytes=media_bytes,
        media_mime=media_mime,
        raw_meta="\n".join(raw_meta_lines),
    )


def _extract_classifier_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def analyze_asset_with_ai(data: AssetInput) -> str:
    """Very cheap first pass: classify asset type only.

    Detailed reconstruction is performed later by semantic_reconstruction_engine.
    This call intentionally avoids theme/entity/layout analysis.
    """
    client = _get_client()
    prompt = f"""
Определи только тип SMM-материала. Не анализируй содержание подробно.

Допустимые content_type:
- infographic: инфографика/таблица/чеклист/схема с несколькими пунктами
- text_post: в основном текст без значимой картинки
- meme: мем/юмор/реакция/картинка с шуткой
- image_post: обычный пост с фото/картинкой, не инфографика
- other: другое или неясно

Источник: {data.source_type}
Ссылка: {data.source_url or 'нет'}
Тип медиа: {data.media_type or 'нет'}
Текст/caption: {_cut((data.text or '') + ' ' + (data.caption or ''), 1200)}
Метаданные: {_cut(data.raw_meta, 600)}

Верни строго JSON без markdown:
{{"content_type":"infographic|text_post|meme|image_post|other","confidence":0.0,"reconstructable":true}}
""".strip()

    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if data.media_bytes and data.media_mime and data.media_mime.startswith("image/"):
        encoded = base64.b64encode(data.media_bytes).decode("utf-8")
        user_content.append({"type": "input_image", "image_url": f"data:{data.media_mime};base64,{encoded}"})

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": "Ты — лёгкий классификатор SMM-контента. Верни только JSON с типом материала."},
            {"role": "user", "content": user_content},
        ],
    )
    result = response.output_text.strip()
    data_json = _extract_classifier_json(result)
    if not data_json:
        data_json = {"content_type": "other", "confidence": 0.0, "reconstructable": False}
    return json.dumps(data_json, ensure_ascii=False, separators=(",", ":"))


def _classifier_summary(analysis: str) -> str:
    data = _extract_classifier_json(analysis)
    ctype = str(data.get("content_type") or "other")
    confidence = data.get("confidence")
    if confidence is None:
        return f"Тип материала: {ctype}"
    return f"Тип материала: {ctype}; confidence: {confidence}"


def _classifier_type(analysis: str) -> str:
    data = _extract_classifier_json(analysis)
    return str(data.get("content_type") or "other")[:500]


def save_asset_pattern_context(db: Session, data: AssetInput, analysis: str) -> tuple[ContentAsset, ContentPattern, ContentContext]:
    asset = ContentAsset(
        source_type=data.source_type,
        source_url=data.source_url,
        text_content=data.text,
        caption=data.caption,
        media_type=data.media_type,
        media_file_id=data.media_file_id,
        raw_meta=data.raw_meta,
        analysis=analysis,
        content_summary=_classifier_summary(analysis),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    pattern = ContentPattern(
        asset_id=asset.id,
        hook_type="classification",
        emotion=None,
        pain_point=None,
        format=_classifier_type(analysis),
        visual_style=None,
        humor_mechanic=None,
        engagement_reason=None,
        cta_type=None,
        content_mechanic=None,
        analysis=analysis,
    )
    db.add(pattern)

    context = ContentContext(
        asset_id=asset.id,
        cultural_context=None,
        timing_reason=None,
        audience=None,
        medical_applicability=None,
        adaptation_risks=None,
        clinic_ideas=None,
        analysis=analysis,
    )
    db.add(context)
    db.commit()
    db.refresh(pattern)
    db.refresh(context)
    return asset, pattern, context


def analyze_and_save_asset(db: Session, data: AssetInput) -> tuple[ContentAsset, ContentPattern, ContentContext]:
    if not (data.text or data.caption or data.media_file_id or data.source_url):
        raise AssetAnalyzeError("Не вижу текста, caption, ссылки или медиа для анализа.")
    analysis = analyze_asset_with_ai(data)
    return save_asset_pattern_context(db, data, analysis)


def list_assets(db: Session, limit: int = 10) -> list[ContentAsset]:
    return db.query(ContentAsset).order_by(ContentAsset.id.desc()).limit(limit).all()


def list_patterns(db: Session, limit: int = 10) -> list[ContentPattern]:
    return db.query(ContentPattern).order_by(ContentPattern.id.desc()).limit(limit).all()


def get_pattern_or_raise(db: Session, pattern_id: int) -> ContentPattern:
    pattern = db.query(ContentPattern).filter(ContentPattern.id == pattern_id).first()
    if not pattern:
        raise PatternNotFoundError(f"Паттерн с ID {pattern_id} не найден.")
    return pattern


def generate_post_from_pattern(db: Session, pattern_id: int, with_image: bool = True) -> ContentPost:
    pattern = get_pattern_or_raise(db, pattern_id)
    asset = db.query(ContentAsset).filter(ContentAsset.id == pattern.asset_id).first() if pattern.asset_id else None
    context = db.query(ContentContext).filter(ContentContext.asset_id == pattern.asset_id).first() if pattern.asset_id else None

    client = _get_client()
    prompt = f"""
Создай оригинальный SMM-пост для медицинской клиники в Москве на основе успешной механики, но НЕ копируй исходный материал.

Исходный контент для понимания:
{_cut(asset.content_summary if asset else '', 1000)}

Паттерн:
- Тип хука: {pattern.hook_type or ''}
- Эмоция: {pattern.emotion or ''}
- Боль/желание: {pattern.pain_point or ''}
- Формат: {pattern.format or ''}
- Визуальный стиль: {pattern.visual_style or ''}
- Механика юмора: {pattern.humor_mechanic or ''}
- Причина вовлечения: {pattern.engagement_reason or ''}

Контекст:
- Культурный контекст: {(context.cultural_context if context else '') or ''}
- Медицинская применимость: {(context.medical_applicability if context else '') or ''}
- Риски: {(context.adaptation_risks if context else '') or ''}
- Идеи для клиники: {(context.clinic_ideas if context else '') or ''}

Требования:
- пост должен быть новым и оригинальным;
- можно использовать юмор только мягко и безопасно;
- без обещаний гарантированного результата;
- без постановки диагноза по симптомам;
- 1200–2000 символов;
- в конце мягкий CTA;
- верни строго:
Тема: ...
Текст: ...
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": "Ты — SMM-копирайтер медицинской клиники. Создавай оригинальный контент на основе паттернов внимания, не копируй исходники."},
            {"role": "user", "content": prompt},
        ],
    )
    result = response.output_text.strip()
    topic = _field(result, "Тема", limit=255) or "Пост на основе успешного паттерна"
    text = _field(result, "Текст", limit=6000) or result
    headline = generate_headline(topic=topic, text=text)

    post = ContentPost(
        title=topic[:255],
        headline=headline,
        platform="telegram",
        text=text,
        status="generated",
        ai_model=settings.openai_model,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if with_image:
        image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=f"Используй визуальную механику: {pattern.visual_style or ''}")
        post.image_path = image_path
        post.image_prompt = image_prompt
        post.image_model = settings.openai_image_model
        db.commit()
        db.refresh(post)

    return post


def format_asset_card(asset: ContentAsset) -> str:
    return "\n".join([
        f"<b>Контент-исходник #{asset.id}</b>",
        f"Источник: {html.escape(asset.source_type or '—')}",
        f"Медиа: {html.escape(asset.media_type or 'нет')}",
        f"Ссылка: {html.escape(asset.source_url or '—')}",
        "",
        f"<b>Кратко:</b> {html.escape(asset.content_summary or '—')}",
    ])


def format_pattern_card(pattern: ContentPattern) -> str:
    return "\n".join([
        f"<b>Паттерн #{pattern.id}</b>",
        f"Исходник: #{pattern.asset_id}",
        f"<b>Хук:</b> {html.escape(pattern.hook_type or '—')}",
        f"<b>Эмоция:</b> {html.escape(pattern.emotion or '—')}",
        f"<b>Боль/желание:</b> {html.escape(pattern.pain_point or '—')}",
        f"<b>Формат:</b> {html.escape(pattern.format or '—')}",
        f"<b>Визуал:</b> {html.escape(pattern.visual_style or '—')}",
        f"<b>Юмор:</b> {html.escape(pattern.humor_mechanic or '—')}",
        f"<b>Почему вовлекает:</b> {html.escape(pattern.engagement_reason or '—')}",
    ])
