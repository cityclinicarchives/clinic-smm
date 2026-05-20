import html
from pathlib import Path

from app.config import settings
from app.models import ContentPost
from app.services.telegram_bot import TelegramBotError, send_message, send_photo


class PublishError(RuntimeError):
    pass


TELEGRAM_CAPTION_LIMIT = 1024
# Оставляем небольшой запас: Telegram считает длину caption с учетом служебной разметки/HTML.
TELEGRAM_CAPTION_SAFE_LIMIT = 950


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
    Caption для короткого варианта: фото + текст одним сообщением.
    Заголовок встроен в изображение, поэтому в caption публикуем только текст.
    """
    if not post.text:
        raise PublishError("У поста нет текста для публикации.")

    return html.escape(post.text.strip())


def _fits_telegram_caption(caption: str) -> bool:
    return len(caption) <= TELEGRAM_CAPTION_SAFE_LIMIT


def _send_post_separator() -> None:
    """
    Отправляет визуальный промежуток между разными постами.

    Важно: разделитель вызывается один раз перед публикацией нового поста,
    но НЕ вызывается между связанными сообщениями одного поста
    (например, картинка + длинный текст).
    """
    if not settings.telegram_publish_separator_enabled:
        return

    separator = (settings.telegram_publish_separator or "").strip()
    if not separator:
        return

    send_message(settings.telegram_publish_chat_id, separator)


def publish_post_to_telegram_test_group(post: ContentPost) -> None:
    """
    Публикует одобренный пост в тестовую Telegram-группу.

    Перед каждым новым постом отправляет отдельное сообщение-разделитель.
    Между связанными сообщениями одного поста разделитель не отправляется.

    Логика публикации:
    1. Если картинки нет:
       - одно сообщение: жирный заголовок + текст.

    2. Если картинка есть и текст короткий:
       - картинка уже сгенерирована ИИ как готовая дизайнерская карточка с заголовком;
       - одно сообщение: фото + caption с текстом.

    3. Если картинка есть и текст длинный:
       - первое сообщение: только картинка, БЕЗ Telegram caption;
       - второе сообщение: жирный заголовок + полный текст поста.
    """
    if not settings.telegram_publish_chat_id:
        raise PublishError(
            "TELEGRAM_PUBLISH_CHAT_ID не задан. Добавьте ID тестовой группы в Railway Variables."
        )

    try:
        _send_post_separator()

        if post.image_path and Path(post.image_path).exists():
            caption = _format_caption_with_image(post)

            if _fits_telegram_caption(caption):
                # Короткий пост: публикуем одним сообщением.
                # Заголовок уже встроен в изображение при генерации ИИ.
                send_photo(
                    chat_id=settings.telegram_publish_chat_id,
                    photo_path=post.image_path,
                    caption=caption,
                )
            else:
                # Длинный пост: строго по логике пользователя.
                # 1) картинка без Telegram caption;
                # 2) отдельное сообщение с заголовком и полным текстом.
                send_photo(
                    chat_id=settings.telegram_publish_chat_id,
                    photo_path=post.image_path,
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
