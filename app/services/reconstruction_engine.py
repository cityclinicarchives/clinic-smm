import html
import re
from typing import Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset, ContentContext, ContentPattern, ContentPost, ContentReconstruction
from app.services.copywriter import generate_headline
from app.services.image_generator import generate_image_for_post


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


def _field(text: str, *names: str, limit: int = 4000) -> str:
    joined = "|".join(re.escape(name) for name in names)
    pattern = rf"(?:^|\n)(?:{joined})\s*:\s*(.+?)(?=\n[A-Za-zА-Яа-яЁё0-9 _/().-]{{2,60}}\s*:|\Z)"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
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


RECONSTRUCTION_SYSTEM_PROMPT = """
Ты — главный медицинский редактор, SMM-стратег и арт-директор частной медицинской клиники в Москве.

Твоя задача — не рерайт и не копирование. Твоя задача — ЭКСПЕРТНАЯ РЕКОНСТРУКЦИЯ успешного контента.

Работай послойно:
1. Исходный контент — что реально сказано/показано.
2. Хук и эмоция — что цепляет внимание.
3. Медицинская корректность — что верно, что сомнительно, что нужно исправить.
4. Недостающая польза — что можно добавить по теме.
5. Смысловая структура — что нужно сохранить, потому что это работает.
6. Новая версия — оригинальная, юридически и медицински безопасная, пригодная для клиники.

Критически важные правила:
- Если исходный заголовок сильный, не меняй его принципиально. Можно только слегка усилить или уточнить.
- Если заголовок слабый, предложи лучший вариант, но сохрани хук и эмоцию.
- Не делай шаблонный рерайт.
- Не делай категоричных диагнозов по симптомам.
- Не обещай лечение или гарантированный результат.
- Для инфографики формируй конкретный текст будущей инфографики: заголовок, блоки, подписи, предупреждения.
- Для юмора сохраняй механику юмора, но не копируй конкретный мем.
- Для медицинского контента всегда добавляй мягкую экспертность: "симптомы могут иметь разные причины", "обсудите с врачом", "анализы помогают уточнить картину".

Верни строго структурированный ответ с указанными заголовками полей.
""".strip()


def reconstruct_asset_with_ai(db: Session, asset_id: int, instruction: str | None = None) -> ContentReconstruction:
    asset = get_asset_or_raise(db, asset_id)
    pattern, context = _get_related_pattern_context(db, asset.id)

    prompt = f"""
Сделай экспертную реконструкцию контент-исходника #{asset.id} для медицинской клиники.

Источник: {asset.source_type or '—'}
Ссылка: {asset.source_url or '—'}
Тип медиа: {asset.media_type or '—'}

Текст исходника:
{_cut(asset.text_content, 5000)}

Caption/подпись исходника:
{_cut(asset.caption, 5000)}

Предыдущий AI-анализ исходника:
{_cut(asset.analysis, 8000)}

Паттерн внимания:
- Хук: {(pattern.hook_type if pattern else '') or ''}
- Эмоция: {(pattern.emotion if pattern else '') or ''}
- Боль/желание: {(pattern.pain_point if pattern else '') or ''}
- Формат: {(pattern.format if pattern else '') or ''}
- Визуальный стиль: {(pattern.visual_style if pattern else '') or ''}
- Юмор: {(pattern.humor_mechanic if pattern else '') or ''}
- Почему вовлекает: {(pattern.engagement_reason if pattern else '') or ''}

Контекст:
- Культурный/сезонный контекст: {(context.cultural_context if context else '') or ''}
- Медицинская применимость: {(context.medical_applicability if context else '') or ''}
- Риски адаптации: {(context.adaptation_risks if context else '') or ''}
- Идеи для клиники: {(context.clinic_ideas if context else '') or ''}

Дополнительная инструкция пользователя:
{instruction or 'нет'}

Верни строго в таком формате:
Тип контента: инфографика / пост / мем / видео-идея / карусель / другое
Оценка заголовка: сильный / средний / слабый + почему
Исходный заголовок: ...
Итоговый заголовок: ...
Что сохраняем: ...
Что исправляем: ...
Медицинский аудит: ...
Добавления по теме: ...
Реконструкция инфографики: если это инфографика или визуальный образовательный пост, напиши готовую структуру будущей инфографики: заголовок, 6-12 блоков, нижний дисклеймер/CTA. Если не инфографика — напиши "не требуется".
Стратегия визуала: ...
Тема поста: ...
Текст поста: ...
Промпт для изображения: подробная инструкция для генерации новой картинки/инфографики, с учетом исправленного содержания, визуального паттерна и медицинской корректности.
""".strip()

    client = _get_client()
    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": RECONSTRUCTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    analysis = response.output_text.strip()

    reconstruction = ContentReconstruction(
        asset_id=asset.id,
        content_type=_field(analysis, "Тип контента", limit=255),
        original_title=_field(analysis, "Исходный заголовок", limit=500),
        final_title=_field(analysis, "Итоговый заголовок", limit=500),
        title_evaluation=_field(analysis, "Оценка заголовка", limit=1000),
        preserved_elements=_field(analysis, "Что сохраняем", limit=2000),
        corrected_elements=_field(analysis, "Что исправляем", limit=2500),
        medical_audit=_field(analysis, "Медицинский аудит", limit=4000),
        additions=_field(analysis, "Добавления по теме", limit=3000),
        infographic_structure=_field(analysis, "Реконструкция инфографики", limit=5000),
        visual_strategy=_field(analysis, "Стратегия визуала", limit=3000),
        post_topic=_field(analysis, "Тема поста", limit=500),
        post_text=_field(analysis, "Текст поста", limit=8000),
        image_prompt=_field(analysis, "Промпт для изображения", limit=6000),
        analysis=analysis,
    )
    db.add(reconstruction)
    db.commit()
    db.refresh(reconstruction)
    return reconstruction


def create_post_from_reconstruction(db: Session, reconstruction_id: int, with_image: bool = True) -> ContentPost:
    reconstruction = db.query(ContentReconstruction).filter(ContentReconstruction.id == reconstruction_id).first()
    if not reconstruction:
        raise ReconstructionNotFoundError(f"Реконструкция с ID {reconstruction_id} не найдена.")

    topic = (reconstruction.post_topic or reconstruction.final_title or "Пост на основе реконструкции")[:255]
    text = reconstruction.post_text or reconstruction.analysis or ""
    headline = (reconstruction.final_title or generate_headline(topic=topic, text=text))[:255]

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
        custom_instruction = f"""
Это изображение создается на основе экспертной реконструкции успешного контента.

Итоговый заголовок: {reconstruction.final_title or headline}

Стратегия визуала:
{reconstruction.visual_strategy or ''}

Исправленная структура инфографики/визуального материала:
{reconstruction.infographic_structure or ''}

Медицинские исправления и добавления, которые нужно учесть:
{reconstruction.medical_audit or ''}
{reconstruction.additions or ''}

Промпт реконструкции:
{reconstruction.image_prompt or ''}

Если это инфографика, создай именно новую чистую медицинскую инфографику, а не обычное фото врача с пациентом.
Сохраняй полезную структуру исходника, но исправь медицинские неточности и сделай оригинальный дизайн.
""".strip()
        image_path, image_prompt = generate_image_for_post(post=post, custom_instruction=custom_instruction)
        post.image_path = image_path
        post.image_prompt = image_prompt
        post.image_model = settings.openai_image_model
        db.commit()
        db.refresh(post)

    return post


def list_reconstructions(db: Session, limit: int = 10) -> list[ContentReconstruction]:
    return db.query(ContentReconstruction).order_by(ContentReconstruction.id.desc()).limit(limit).all()


def format_reconstruction_card(item: ContentReconstruction) -> str:
    return "\n".join([
        f"<b>Реконструкция #{item.id}</b>",
        f"Исходник: #{item.asset_id}",
        f"Тип: {html.escape(item.content_type or '—')}",
        f"Пост: #{item.created_post_id}" if item.created_post_id else "Пост: еще не создан",
        "",
        f"<b>Итоговый заголовок:</b> {html.escape(item.final_title or '—')}",
        f"<b>Оценка заголовка:</b> {html.escape(item.title_evaluation or '—')}",
        "",
        f"<b>Что исправляем:</b> {html.escape(_cut(item.corrected_elements, 900) or '—')}",
        f"<b>Добавления:</b> {html.escape(_cut(item.additions, 900) or '—')}",
        "",
        f"<b>Визуальная стратегия:</b> {html.escape(_cut(item.visual_strategy, 1000) or '—')}",
    ])
