import html
from pathlib import Path

from app.config import settings
from app.models import ContentPost
from app.services.image_text import add_headline_to_image
from app.services.telegram_bot import TelegramBotError, send_message, send_photo


class PublishError(RuntimeError):
    pass


TELEGRAM_CAPTION_LIMIT = 1024


def _safe_headline(post: ContentPost) -> str:
    return (post.headline or post.title or "").strip()


def _format_text_with_headline(post: ContentPost) -> str:
    """
    Формирует HTML-текст для Telegram:
    - заголовок жирным;
    - ниже полный текст поста.
    """
    if not post.text:
        raise PublishError("У поста нет текста для публикации.")

    headline = _safe_headline(post)
    text = post.text.strip()

    if headline:
        return f"<b>{html.escape(headline)}</b>\n\n{html.escape(text)}"
    return html.escape(text)


def _format_caption_with_image(post: ContentPost) -> str:
    """
    Caption для варианта, когда фото и текст помещаются в одно сообщение.
    Заголовок уже встроен в изображение, поэтому в caption публикуем только текст.
    """
    if not post.text:
        raise PublishError("У поста нет текста для публикации.")

    return html.escape(post.text.strip())


def publish_post_to_telegram_test_group(post: ContentPost) -> None:
    """
    Публикует одобренный пост в тестовую Telegram-группу.

    Логика публикации:
    1. Если картинки нет:
       - отправляем одно сообщение: жирный заголовок + текст.

    2. Если картинка есть и текст помещается в Telegram caption:
       - встраиваем заголовок в изображение;
       - отправляем одно сообщение: фото + caption с текстом.

    3. Если картинка есть, но текст длиннее лимита caption:
       - встраиваем заголовок в изображение;
       - отправляем первое сообщение: только фото;
       - отправляем второе сообщение: жирный заголовок + полный текст.
    """
    if not settings.telegram_publish_chat_id:
        raise PublishError(
            "TELEGRAM_PUBLISH_CHAT_ID не задан. Добавьте ID тестовой группы в Railway Variables."
        )

    try:
        if post.image_path and Path(post.image_path).exists():
            photo_path = add_headline_to_image(post.image_path, _safe_headline(post))
            caption = _format_caption_with_image(post)

            if len(caption) <= TELEGRAM_CAPTION_LIMIT:
                send_photo(
                    chat_id=settings.telegram_publish_chat_id,
                    photo_path=photo_path,
                    caption=caption,
                )
            else:
                # Telegram ограничивает caption примерно 1024 символами.
                # Поэтому длинные посты публикуем двумя сообщениями:
                # 1) картинка с заголовком внутри изображения;
                # 2) полный текст поста с жирным заголовком.
                send_photo(
                    chat_id=settings.telegram_publish_chat_id,
                    photo_path=photo_path,
                    caption=None,
                )
                send_message(
                    settings.telegram_publish_chat_id,
                    _format_text_with_headline(post),
                )
        else:
            send_message(settings.telegram_publish_chat_id, _format_text_with_headline(post))
    except TelegramBotError as exc:
        raise PublishError(str(exc)) from exc
