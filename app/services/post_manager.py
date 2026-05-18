from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentPost
from app.services.copywriter import generate_post_text, rewrite_post_text
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
) -> ContentPost:
    generated_text = generate_post_text(
        topic=topic,
        platform=platform,
        service_offer=service_offer,
    )

    post = ContentPost(
        title=topic,
        platform=platform,
        text=generated_text,
        status="generated",
        ai_model=settings.openai_model,
    )
    db.add(post)
    db.commit()
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
    post.text = new_text
    post.status = "edited"
    db.commit()
    db.refresh(post)
    return post


def rewrite_post_with_ai(db: Session, post_id: int, instruction: str) -> ContentPost:
    post = get_post_or_raise(db, post_id)
    if not post.text:
        raise RuntimeError("У поста нет текста для редактирования.")

    post.text = rewrite_post_text(
        original_text=post.text,
        instruction=instruction,
        platform=post.platform,
    )
    post.status = "edited"
    post.ai_model = settings.openai_model
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
