import html

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.post_manager import (
    PostNotFoundError,
    PostStatusError,
    approve_post,
    create_generated_post,
    edit_post_manually,
    generate_or_replace_image,
    get_post_or_raise,
    list_recent_posts,
    publish_post,
    reject_post,
    rewrite_post_with_ai,
)
from app.services.telegram_bot import (
    answer_callback_query,
    get_webhook_info,
    send_message,
    send_photo,
    set_webhook,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _is_admin(chat_id: int | str) -> bool:
    if not settings.admin_telegram_id:
        return True
    return str(chat_id) == str(settings.admin_telegram_id)


def _safe(text: str | None) -> str:
    return html.escape(text or "")


def _shorten(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n...текст обрезан, полный пост сохранен в базе."


def _post_keyboard(post_id: int) -> dict:
    """Кнопки под конкретным постом.

    Используем callback_data, поэтому администратору не нужно копировать команды вида /publish 4.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve:{post_id}"},
                {"text": "🚀 Опубликовать", "callback_data": f"publish:{post_id}"},
            ],
            [
                {"text": "🖼 Картинка", "callback_data": f"image:{post_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{post_id}"},
            ],
            [
                {"text": "👁 Показать пост", "callback_data": f"show:{post_id}"},
                {"text": "✏️ Как редактировать", "callback_data": f"edit_help:{post_id}"},
            ],
        ]
    }


def _post_card(post, include_text: bool = False) -> str:
    lines = [
        f"<b>Пост #{post.id}</b>",
        f"Тема: {_safe(post.title)}",
        f"Заголовок: {_safe(getattr(post, 'headline', None) or '—')}",
        f"Платформа: {_safe(post.platform)}",
        f"Статус: {_safe(post.status)}",
        f"Изображение: {'есть' if getattr(post, 'image_path', None) else 'нет'}",
    ]
    if include_text:
        lines.append("")
        lines.append(_safe(_shorten(post.text or "Текст пустой.")))
        lines.append("")
        lines.append("Кнопки ниже выполняют действия без копирования команд.")
        lines.append("")
        lines.append("Для ручной правки напишите отдельным сообщением:")
        lines.append(f"/edit {post.id} новый полный текст поста")
        lines.append("")
        lines.append("Для ИИ-редактирования:")
        lines.append(f"/rewrite {post.id} что нужно исправить")
    return "\n".join(lines)


def _parse_id_and_payload(text: str, command: str) -> tuple[int | None, str]:
    rest = text.replace(command, "", 1).strip()
    if not rest:
        return None, ""
    parts = rest.split(maxsplit=1)
    try:
        post_id = int(parts[0])
    except ValueError:
        return None, rest
    payload = parts[1].strip() if len(parts) > 1 else ""
    return post_id, payload


def _send_post(chat_id: int | str, post, include_text: bool = True) -> None:
    send_message(chat_id, _post_card(post, include_text=include_text), reply_markup=_post_keyboard(post.id))


def _handle_callback(callback_query: dict, db: Session) -> dict:
    callback_query_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    data = callback_query.get("data") or ""

    if callback_query_id:
        answer_callback_query(callback_query_id)

    if not chat_id:
        return {"ok": True}

    if not _is_admin(chat_id):
        send_message(chat_id, "Нет доступа к этому боту.")
        return {"ok": True}

    try:
        action, raw_post_id = data.split(":", 1)
        post_id = int(raw_post_id)
    except Exception:
        send_message(chat_id, "Не удалось распознать кнопку.")
        return {"ok": True}

    try:
        if action == "show":
            post = get_post_or_raise(db, post_id)
            _send_post(chat_id, post, include_text=True)

        elif action == "approve":
            post = approve_post(db, post_id)
            send_message(chat_id, f"✅ Пост #{post.id} одобрен.", reply_markup=_post_keyboard(post.id))

        elif action == "publish":
            post = publish_post(db, post_id)
            send_message(chat_id, f"🚀 Пост #{post.id} опубликован в тестовую группу. Статус: published")

        elif action == "reject":
            post = reject_post(db, post_id)
            send_message(chat_id, f"❌ Пост #{post.id} отклонен. Статус: rejected", reply_markup=_post_keyboard(post.id))

        elif action == "image":
            send_message(chat_id, f"Генерирую изображение для поста #{post_id}.")
            post = generate_or_replace_image(db, post_id, None)
            send_message(chat_id, f"🖼 Изображение для поста #{post.id} создано/обновлено.", reply_markup=_post_keyboard(post.id))
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "edit_help":
            send_message(
                chat_id,
                "Для ручного редактирования отправьте:\n"
                f"<code>/edit {post_id} здесь полный исправленный текст поста</code>\n\n"
                "Для ИИ-редактирования отправьте:\n"
                f"<code>/rewrite {post_id} сделай текст короче, убери повторы и добавь мягкий призыв записаться</code>",
            )

        else:
            send_message(chat_id, "Неизвестное действие кнопки.")

    except (PostNotFoundError, PostStatusError, RuntimeError) as exc:
        send_message(chat_id, _safe(str(exc)))
    except Exception as exc:
        send_message(chat_id, f"Ошибка:\n{_safe(str(exc))}")

    return {"ok": True}


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()

    callback_query = update.get("callback_query")
    if callback_query:
        return _handle_callback(callback_query, db)

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    if not _is_admin(chat_id):
        send_message(chat_id, "Нет доступа к этому боту.")
        return {"ok": True}

    if text == "/start" or text == "/help":
        send_message(
            chat_id,
            "<b>Бот SMM-менеджера работает.</b>\n\n"
            "Команды:\n"
            "/generate тема — создать пост без изображения\n"
            "/generate_full тема — создать пост + изображение\n"
            "/posts — последние посты\n"
            "/post ID — посмотреть пост\n"
            "/edit ID новый текст — заменить текст вручную\n"
            "/rewrite ID инструкция — отредактировать через ИИ\n\n"
            "Одобрение, отклонение, генерация изображения и публикация теперь доступны кнопками под постом.",
        )
        return {"ok": True}

    if text == "/posts":
        posts = list_recent_posts(db, limit=10)
        if not posts:
            send_message(chat_id, "Постов пока нет.")
            return {"ok": True}

        send_message(chat_id, "<b>Последние посты:</b>")
        for post in posts:
            send_message(
                chat_id,
                f"#{post.id} | {_safe(post.platform)} | {_safe(post.status)} | {_safe(getattr(post, 'headline', None) or post.title)}",
                reply_markup=_post_keyboard(post.id),
            )
        return {"ok": True}

    if text.startswith("/post"):
        post_id, _ = _parse_id_and_payload(text, "/post")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /post 1")
            return {"ok": True}
        try:
            post = get_post_or_raise(db, post_id)
            _send_post(chat_id, post, include_text=True)
        except PostNotFoundError as exc:
            send_message(chat_id, str(exc))
        return {"ok": True}

    if text.startswith("/generate_full"):
        topic = text.replace("/generate_full", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate_full Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост и изображение.")

        try:
            post = create_generated_post(
                db=db,
                topic=topic,
                platform="telegram",
                service_offer=None,
                with_image=True,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "Пост и изображение созданы и сохранены в базе.")
        _send_post(chat_id, post, include_text=True)
        if post.image_path:
            try:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
            except Exception as exc:
                send_message(chat_id, f"Изображение создано, но не удалось отправить превью:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/generate"):
        topic = text.replace("/generate", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост.")

        try:
            post = create_generated_post(
                db=db,
                topic=topic,
                platform="telegram",
                service_offer=None,
                with_image=False,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "Пост создан и сохранен в базе.")
        _send_post(chat_id, post, include_text=True)
        return {"ok": True}

    if text.startswith("/approve"):
        post_id, _ = _parse_id_and_payload(text, "/approve")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /approve 1")
            return {"ok": True}
        try:
            post = approve_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} одобрен.", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/publish"):
        post_id, _ = _parse_id_and_payload(text, "/publish")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /publish 1")
            return {"ok": True}
        try:
            post = publish_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} опубликован в тестовую группу. Статус: published")
        except (PostNotFoundError, PostStatusError, RuntimeError) as exc:
            send_message(chat_id, _safe(str(exc)))
        except Exception as exc:
            send_message(chat_id, f"Ошибка публикации:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/image"):
        post_id, instruction = _parse_id_and_payload(text, "/image")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например:\n/image 1")
            return {"ok": True}

        send_message(chat_id, "Генерирую изображение.")
        try:
            post = generate_or_replace_image(db, post_id, instruction or None)
            send_message(chat_id, f"Изображение для поста #{post.id} создано/обновлено.", reply_markup=_post_keyboard(post.id))
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации изображения:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/reject"):
        post_id, _ = _parse_id_and_payload(text, "/reject")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /reject 1")
            return {"ok": True}
        try:
            post = reject_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} отклонен. Статус: rejected", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/edit"):
        post_id, new_text = _parse_id_and_payload(text, "/edit")
        if not post_id or not new_text:
            send_message(chat_id, "Формат команды:\n/edit 1 Полный новый текст поста")
            return {"ok": True}
        try:
            post = edit_post_manually(db, post_id, new_text)
            send_message(chat_id, "Пост отредактирован вручную.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/rewrite"):
        post_id, instruction = _parse_id_and_payload(text, "/rewrite")
        if not post_id or not instruction:
            send_message(chat_id, "Формат команды:\n/rewrite 1 Сделай текст короче и убери повторы")
            return {"ok": True}

        send_message(chat_id, "Редактирую пост через ИИ.")
        try:
            post = rewrite_post_with_ai(db, post_id, instruction)
            send_message(chat_id, "Пост отредактирован через ИИ.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    send_message(chat_id, "Команда не распознана. Напишите /help")
    return {"ok": True}


@router.post("/set-webhook")
def setup_telegram_webhook():
    if not settings.public_base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL не задан. Добавьте PUBLIC_BASE_URL в Railway Variables.",
        )

    webhook_url = settings.public_base_url.rstrip("/") + "/telegram/webhook"
    try:
        return set_webhook(webhook_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/webhook-info")
def telegram_webhook_info():
    try:
        return get_webhook_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
