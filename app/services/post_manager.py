from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentPost
from app.services.copywriter import generate_headline, generate_post_text, rewrite_post_text
from app.services.image_generator import generate_image_for_post
from app.services.publisher import publish_post_to_telegram_test_group


class PostNotFoundError(RuntimeError):
    pass


class PostStatusError(RuntimeError):
    pass


def get_post_or_raise(db: Session, post_id: int) -> ContentPost:
    post = db.query(ContentPost).filter(ContentPost.id == post_id).first()
    if not post:
        raise PostNotFoundError(f"Пост с ID {post_id} не найден.")
    return post


def list_recent_posts(db: Session, limit: int = 10) -> list[ContentPost]:
    return db.query(ContentPost).order_by(ContentPost.id.desc()).limit(limit).all()


def create_generated_post(
    db: Session,
    topic: str,
    platform: str = "telegram",
    service_offer: str | None = None,
    with_image: bool = False,
) -> ContentPost:
    generated_text = generate_post_text(
        topic=topic,
        platform=platform,
        service_offer=service_offer,
    )

    headline = generate_headline(topic=topic, text=generated_text)

    post = ContentPost(
        title=topic,
        headline=headline,
        platform=platform,
        text=generated_text,
        status="generated",
        ai_model=settings.openai_model,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    if with_image:
        generate_or_replace_image(db, post.id)
        db.refresh(post)

    return post


def approve_post(db: Session, post_id: int) -> ContentPost:
    post = get_post_or_raise(db, post_id)
    post.status = "approved"
    db.commit()
    db.refresh(post)
    return post


def reject_post(db: Session, post_id: int) -> ContentPost:
    post = get_post_or_raise(db, post_id)
    post.status = "rejected"
    db.commit()
    db.refresh(post)
    return post


def edit_post_manually(db: Session, post_id: int, new_text: str) -> ContentPost:
    post = get_post_or_raise(db, post_id)
    # ВАЖНО: меняем только текст, заголовок и статус.
    # Поля image_path/image_prompt/image_model не трогаем, чтобы ранее созданная картинка не терялась.
    post.text = new_text
    post.headline = generate_headline(topic=post.title, text=new_text)
    post.status = "edited"
    db.commit()
    db.refresh(post)
    return post


def rewrite_post_with_ai(db: Session, post_id: int, instruction: str) -> ContentPost:
    post = get_post_or_raise(db, post_id)
    if not post.text:
        raise RuntimeError("У поста нет текста для редактирования.")

    # ВАЖНО: ИИ-редактирование не должно удалять картинку.
    # Поэтому ниже меняем только текст, заголовок, статус и модель.
    post.text = rewrite_post_text(
        original_text=post.text,
        instruction=instruction,
        platform=post.platform,
    )
    post.headline = generate_headline(topic=post.title, text=post.text)
    post.status = "edited"
    post.ai_model = settings.openai_model
    db.commit()
    db.refresh(post)
    return post


def generate_or_replace_image(
    db: Session,
    post_id: int,
    custom_instruction: str | None = None,
) -> ContentPost:
    post = get_post_or_raise(db, post_id)

    if not post.headline:
        post.headline = generate_headline(topic=post.title, text=post.text)

    image_path, image_prompt = generate_image_for_post(
        post=post,
        custom_instruction=custom_instruction,
    )

    post.image_path = image_path
    post.image_prompt = image_prompt
    post.image_model = settings.openai_image_model

    # Если пост был уже одобрен, после смены картинки возвращаем на проверку.
    if post.status == "approved":
        post.status = "edited"

    db.commit()
    db.refresh(post)
    return post


def publish_post(db: Session, post_id: int) -> ContentPost:
    post = get_post_or_raise(db, post_id)

    if post.status != "approved":
        raise PostStatusError(
            f"Публиковать можно только одобренные посты. Сейчас статус: {post.status}. "
            f"Сначала выполните /approve {post.id}"
        )

    publish_post_to_telegram_test_group(post)

    post.status = "published"
    db.commit()
    db.refresh(post)
    return post


def select_post_image_version(db: Session, post_id: int, version: str) -> ContentPost:
    """Switch post image between v30 technical draft and polished version.

    The alternative paths are stored inside post.image_prompt by the component
    infographic engine. This avoids adding new DB columns during MVP iterations.
    """
    post = get_post_or_raise(db, post_id)
    prompt = post.image_prompt or ""
    target_key = "technical_draft" if version == "draft" else "polished_image"
    target_path = None
    for line in prompt.splitlines():
        if line.startswith(target_key + "="):
            target_path = line.split("=", 1)[1].strip()
            break
    if not target_path:
        raise RuntimeError(f"Для поста #{post_id} не найдена версия изображения: {version}.")
    post.image_path = target_path
    if post.status == "approved":
        post.status = "edited"
    db.commit()
    db.refresh(post)
    return post
