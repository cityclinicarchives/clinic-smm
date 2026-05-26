import base64
import html
import re
from dataclasses import dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentInspiration, ContentPlanItem
from app.services.telegram_bot import download_file_bytes


class InspirationNotFoundError(RuntimeError):
    pass


class InspirationAnalyzeError(RuntimeError):
    pass


@dataclass
class InspirationInput:
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
        raise InspirationAnalyzeError(
            "OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables."
        )
    return OpenAI(api_key=settings.openai_api_key)


def _safe_cut(text: str | None, limit: int = 6000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...обрезано"


def _extract_jsonish_fields(analysis: str) -> dict[str, str]:
    """Простое извлечение полей из структурированного текста ИИ."""
    fields = {
        "idea": "",
        "format": "",
        "hook": "",
        "why_it_works": "",
        "clinic_service": "",
        "risks": "",
        "recommended_topic": "",
    }
    patterns = {
        "idea": r"(?:Идея|Idea)\s*:\s*(.+)",
        "format": r"(?:Формат|Format)\s*:\s*(.+)",
        "hook": r"(?:Хук|Hook)\s*:\s*(.+)",
        "why_it_works": r"(?:Почему сработало|Почему может сработать|Why it works)\s*:\s*(.+)",
        "clinic_service": r"(?:Услуга клиники|Clinic service)\s*:\s*(.+)",
        "risks": r"(?:Риски|Risks)\s*:\s*(.+)",
        "recommended_topic": r"(?:Рекомендованная тема|Новая тема|Recommended topic)\s*:\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, analysis, flags=re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()[:1000]
    return fields


def _build_analysis_prompt(data: InspirationInput) -> str:
    return f"""
Проанализируй успешный медицинский SMM-пост как источник вдохновения для клиники в Москве.

Важно:
- не копируй пост;
- не пересказывай его дословно;
- извлеки идею, механику, формат, визуальный прием и причину успеха;
- предложи новую оригинальную тему для нашей клиники;
- учитывай медицинскую корректность и ограничения рекламы медицинских услуг.

Источник: {data.source_type}
Ссылка: {data.source_url or 'нет'}
Тип медиа: {data.media_type or 'нет'}

Текст:
{_safe_cut(data.text)}

Caption/подпись:
{_safe_cut(data.caption)}

Метаданные:
{_safe_cut(data.raw_meta)}

Верни строго в таком формате:
Идея: ...
Формат: ...
Хук: ...
Визуальный стиль: ...
Почему сработало: ...
Услуга клиники: ...
Риски: ...
Рекомендованная тема: ...
""".strip()


def analyze_inspiration_with_ai(data: InspirationInput) -> str:
    client = _get_client()
    prompt = _build_analysis_prompt(data)

    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if data.media_bytes and data.media_mime and data.media_mime.startswith("image/"):
        encoded = base64.b64encode(data.media_bytes).decode("utf-8")
        user_content.append({
            "type": "input_image",
            "image_url": f"data:{data.media_mime};base64,{encoded}",
        })

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {
                "role": "system",
                "content": "Ты — SMM-аналитик медицинской клиники. Анализируй контент как inspiration, не копируй чужие материалы.",
            },
            {"role": "user", "content": user_content},
        ],
    )
    return response.output_text.strip()


def save_inspiration(db: Session, data: InspirationInput, analysis: str) -> ContentInspiration:
    fields = _extract_jsonish_fields(analysis)
    inspiration = ContentInspiration(
        source_type=data.source_type,
        source_url=data.source_url,
        original_text=(data.text or data.caption or "")[:6000],
        media_type=data.media_type,
        media_file_id=data.media_file_id,
        analysis=analysis,
        idea=fields["idea"] or None,
        format=fields["format"] or None,
        hook=fields["hook"] or None,
        why_it_works=fields["why_it_works"] or None,
        clinic_service=fields["clinic_service"] or None,
        risks=fields["risks"] or None,
        recommended_topic=fields["recommended_topic"] or None,
    )
    db.add(inspiration)
    db.commit()
    db.refresh(inspiration)
    return inspiration


def create_inspiration(db: Session, data: InspirationInput) -> ContentInspiration:
    analysis = analyze_inspiration_with_ai(data)
    return save_inspiration(db, data, analysis)


def list_inspirations(db: Session, limit: int = 10) -> list[ContentInspiration]:
    return db.query(ContentInspiration).order_by(ContentInspiration.id.desc()).limit(limit).all()


def get_inspiration_or_raise(db: Session, inspiration_id: int) -> ContentInspiration:
    item = db.query(ContentInspiration).filter(ContentInspiration.id == inspiration_id).first()
    if not item:
        raise InspirationNotFoundError(f"Карточка вдохновения с ID {inspiration_id} не найдена.")
    return item


def fetch_url_preview(url: str) -> InspirationInput:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ClinicSMMBot/1.0; +https://example.com/bot)"
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    def meta_value(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return str(tag.get("content")).strip()
        return ""

    title = meta_value("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_value("og:description", "description", "twitter:description")
    image = meta_value("og:image", "twitter:image")

    page_text = soup.get_text("\n")
    page_text = "\n".join([line.strip() for line in page_text.splitlines() if line.strip()])

    raw_meta = "\n".join([
        f"title: {title}",
        f"description: {description}",
        f"image: {image}",
        f"final_url: {str(response.url)}",
    ])

    return InspirationInput(
        source_type="url",
        source_url=url,
        text=_safe_cut(page_text, 5000),
        caption=description,
        media_type="url_preview_image" if image else None,
        raw_meta=raw_meta,
    )


def build_inspiration_input_from_telegram_message(message: dict) -> InspirationInput:
    text = message.get("text") or ""
    caption = message.get("caption") or ""
    source_url = None

    # Если в тексте есть ссылка, сохраняем ее как source_url, но анализируем весь пересланный материал.
    combined = f"{text}\n{caption}"
    url_match = re.search(r"https?://\S+", combined)
    if url_match:
        source_url = url_match.group(0).rstrip(".,);]")

    media_type = None
    media_file_id = None
    media_bytes = None
    media_mime = None

    if message.get("photo"):
        media_type = "photo"
        largest_photo = message["photo"][-1]
        media_file_id = largest_photo.get("file_id")
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
        if media_mime and media_mime.startswith("image/") and media_file_id:
            try:
                media_bytes = download_file_bytes(media_file_id)
            except Exception:
                media_bytes = None

    raw_meta = []
    if message.get("forward_origin"):
        raw_meta.append(f"forward_origin: {message.get('forward_origin')}")
    if message.get("forward_from_chat"):
        raw_meta.append(f"forward_from_chat: {message.get('forward_from_chat')}")
    if message.get("date"):
        raw_meta.append(f"telegram_date: {message.get('date')}")

    return InspirationInput(
        source_type="telegram_forward_or_manual",
        source_url=source_url,
        text=text,
        caption=caption,
        media_type=media_type,
        media_file_id=media_file_id,
        media_bytes=media_bytes,
        media_mime=media_mime,
        raw_meta="\n".join(raw_meta),
    )


def generate_week_plan_from_inspirations(db: Session, platform: str = "telegram") -> list[ContentPlanItem]:
    inspirations = list_inspirations(db, limit=20)
    if not inspirations:
        raise InspirationAnalyzeError("Нет карточек вдохновения. Сначала добавьте пост через /inspire или /analyze_url.")

    client = _get_client()
    inspiration_text = "\n\n".join([
        f"Карточка #{item.id}\nИдея: {item.idea or ''}\nФормат: {item.format or ''}\nХук: {item.hook or ''}\nПочему сработало: {item.why_it_works or ''}\nУслуга: {item.clinic_service or ''}\nРекомендованная тема: {item.recommended_topic or ''}"
        for item in inspirations
    ])

    prompt = f"""
На основании карточек вдохновения составь контент-план на 7 дней для медицинской клиники в Москве.

Правила:
- не копируй исходные посты;
- бери только механику, формат и идею;
- темы должны быть оригинальными;
- медицински корректно;
- без кликбейта;
- платформа: {platform};
- верни строго 7 строк, каждая строка — одна тема, без нумерации.

Карточки вдохновения:
{inspiration_text}
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": "Ты — SMM-стратег медицинской клиники."},
            {"role": "user", "content": prompt},
        ],
    )

    topics = []
    for line in response.output_text.splitlines():
        cleaned = line.strip().lstrip("-•0123456789. )\t").strip().strip('"«»')
        if cleaned:
            topics.append(cleaned)
    topics = topics[:7]

    from datetime import date, timedelta
    today = date.today()
    items: list[ContentPlanItem] = []
    for index, topic in enumerate(topics):
        item = ContentPlanItem(
            planned_date=(today + timedelta(days=index)).isoformat(),
            topic=topic,
            platform=platform,
            status="planned",
            source="inspirations",
        )
        db.add(item)
        items.append(item)

    db.commit()
    for item in items:
        db.refresh(item)
    return items


def format_inspiration_card(item: ContentInspiration) -> str:
    return "\n".join([
        f"<b>Карточка вдохновения #{item.id}</b>",
        f"Источник: {html.escape(item.source_type or '—')}",
        f"Медиа: {html.escape(item.media_type or 'нет')}",
        f"Ссылка: {html.escape(item.source_url or '—')}",
        "",
        f"<b>Идея:</b> {html.escape(item.idea or '—')}",
        f"<b>Формат:</b> {html.escape(item.format or '—')}",
        f"<b>Хук:</b> {html.escape(item.hook or '—')}",
        f"<b>Почему сработало:</b> {html.escape(item.why_it_works or '—')}",
        f"<b>Услуга клиники:</b> {html.escape(item.clinic_service or '—')}",
        f"<b>Риски:</b> {html.escape(item.risks or '—')}",
        f"<b>Новая тема:</b> {html.escape(item.recommended_topic or '—')}",
    ])
