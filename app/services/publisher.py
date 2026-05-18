from app.config import settings
from app.models import ContentPost
from app.services.telegram_bot import TelegramBotError, send_message


class PublishError(RuntimeError):
    pass


def _format_post_for_publication(post: ContentPost) -> str:
    if not post.text:
        raise PublishError("У поста нет текста для публикации.")
    return post.text.strip()


def publish_post_to_telegram_test_group(post: ContentPost) -> None:
    """
    Публикует одобренный пост в тестовую Telegram-группу.

    Позже эту же переменную TELEGRAM_PUBLISH_CHAT_ID можно заменить
    с ID тестовой группы на ID настоящего Telegram-канала клиники.
    """
    if not settings.telegram_publish_chat_id:
        raise PublishError(
            "TELEGRAM_PUBLISH_CHAT_ID не задан. Добавьте ID тестовой группы в Railway Variables."
        )

    text = _format_post_for_publication(post)

    try:
        send_message(settings.telegram_publish_chat_id, text)
    except TelegramBotError as exc:
        raise PublishError(str(exc)) from exc
