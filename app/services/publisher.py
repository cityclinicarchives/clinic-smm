from pathlib import Path

from app.config import settings
from app.models import ContentPost
from app.services.telegram_bot import TelegramBotError, send_message, send_photo


class PublishError(RuntimeError):
    pass


def _format_post_for_publication(post: ContentPost) -> str:
    if not post.text:
        raise PublishError("У поста нет текста для публикации.")
    return post.text.strip()


def publish_post_to_telegram_test_group(post: ContentPost) -> None:
    """
    Публикует одобренный пост в тестовую Telegram-группу.

    Если у поста есть image_path — отправляет фото + текст.
    Если фото нет — отправляет только текст.
    """
    if not settings.telegram_publish_chat_id:
        raise PublishError(
            "TELEGRAM_PUBLISH_CHAT_ID не задан. Добавьте ID тестовой группы в Railway Variables."
        )

    text = _format_post_for_publication(post)

    try:
        if post.image_path and Path(post.image_path).exists():
            # Caption в Telegram ограничен 1024 символами, поэтому фото отправляем с короткой подписью,
            # а полный пост — отдельным сообщением.
            send_photo(
                chat_id=settings.telegram_publish_chat_id,
                photo_path=post.image_path,
                caption=f"{post.title}"[:1024],
            )
            send_message(settings.telegram_publish_chat_id, text)
        else:
            send_message(settings.telegram_publish_chat_id, text)
    except TelegramBotError as exc:
        raise PublishError(str(exc)) from exc
