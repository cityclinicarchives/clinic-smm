import html

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.content_plan import (
    PlanItemNotFoundError,
    PlanItemStatusError,
    create_post_from_plan_item,
    generate_week_plan,
    list_plan_items,
)
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

# Простое состояние диалога в памяти процесса.
# Для одного админа и одного Railway-инстанса этого достаточно на текущем этапе.
# Позже перенесем в PostgreSQL/Redis.
PENDING_ACTIONS: dict[str, dict] = {}


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
    """Кнопки под конкретным постом."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve:{post_id}"},
                {"text": "🚀 Опубликовать", "callback_data": f"publish:{post_id}"},
            ],
            [
                {"text": "✏️ Редактировать вручную", "callback_data": f"edit_manual:{post_id}"},
            ],
            [
                {"text": "🤖 ИИ-редактирование", "callback_data": f"rewrite_ai:{post_id}"},
                {"text": "🖼 Картинка", "callback_data": f"image:{post_id}"},
            ],
            [
                {"text": "❌ Отклонить", "callback_data": f"reject:{post_id}"},
            ],
        ]
    }


def _plan_keyboard(item_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📝 Создать пост", "callback_data": f"plan_create:{item_id}"},
                {"text": "🖼 Создать пост + картинку", "callback_data": f"plan_create_full:{item_id}"},
            ]
        ]
    }


def _plan_item_card(item) -> str:
    post_part = f"Пост: #{item.created_post_id}" if item.created_post_id else "Пост: еще не создан"
    return "\n".join([
        f"<b>Пункт плана #{item.id}</b>",
        f"Дата: {_safe(item.planned_date or '—')}",
        f"Платформа: {_safe(item.platform)}",
        f"Статус: {_safe(item.status)}",
        post_part,
        "",
        f"<b>Тема:</b> {_safe(item.topic)}",
    ])


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
        lines.append("Для действий используйте кнопки под сообщением.")
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


def _set_pending(chat_id: int | str, mode: str, post_id: int) -> None:
    PENDING_ACTIONS[str(chat_id)] = {"mode": mode, "post_id": post_id}


def _clear_pending(chat_id: int | str) -> None:
    PENDING_ACTIONS.pop(str(chat_id), None)


def _get_pending(chat_id: int | str) -> dict | None:
    return PENDING_ACTIONS.get(str(chat_id))


def _handle_pending_text(chat_id: int | str, text: str, db: Session) -> bool:
    """Обрабатывает ответ пользователя после кнопок редактирования.

    Возвращает True, если сообщение было обработано как продолжение диалога.
    """
    pending = _get_pending(chat_id)
    if not pending:
        return False

    # Команды не считаем ответом на редактирование, чтобы пользователь мог отменить/выполнить другую команду.
    if text.startswith("/"):
        if text == "/cancel":
            _clear_pending(chat_id)
            send_message(chat_id, "Редактирование отменено.")
            return True
        return False

    mode = pending.get("mode")
    post_id = int(pending.get("post_id"))

    try:
        if mode == "manual_edit":
            post = edit_post_manually(db, post_id, text)
            _clear_pending(chat_id)
            send_message(
                chat_id,
                "✅ Текст поста заменен вручную. Ранее созданная картинка сохранена.",
            )
            _send_post(chat_id, post, include_text=True)
            return True

        if mode == "ai_rewrite":
            send_message(chat_id, "🤖 Отправляю правки в ИИ.")
            post = rewrite_post_with_ai(db, post_id, text)
            _clear_pending(chat_id)
            send_message(
                chat_id,
                "✅ Пост отредактирован через ИИ. Ранее созданная картинка сохранена.",
            )
            _send_post(chat_id, post, include_text=True)
            return True

    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"Ошибка редактирования:\n{_safe(str(exc))}")
        return True

    _clear_pending(chat_id)
    return False


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
        if action == "approve":
            _clear_pending(chat_id)
            post = approve_post(db, post_id)
            send_message(chat_id, f"✅ Пост #{post.id} одобрен.", reply_markup=_post_keyboard(post.id))

        elif action == "publish":
            _clear_pending(chat_id)
            post = publish_post(db, post_id)
            send_message(chat_id, f"🚀 Пост #{post.id} опубликован в тестовую группу. Статус: published")

        elif action == "reject":
            _clear_pending(chat_id)
            post = reject_post(db, post_id)
            send_message(chat_id, f"❌ Пост #{post.id} отклонен. Статус: rejected", reply_markup=_post_keyboard(post.id))

        elif action == "image":
            _clear_pending(chat_id)
            send_message(chat_id, f"Генерирую изображение для поста #{post_id}.")
            post = generate_or_replace_image(db, post_id, None)
            send_message(chat_id, f"🖼 Изображение для поста #{post.id} создано/обновлено.", reply_markup=_post_keyboard(post.id))
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "edit_manual":
            post = get_post_or_raise(db, post_id)
            _set_pending(chat_id, "manual_edit", post.id)
            send_message(
                chat_id,
                f"✏️ Ручное редактирование поста #{post.id}.\n\n"
                "Введите полную исправленную версию текста одним следующим сообщением.\n\n"
                "Картинка поста будет сохранена. Для отмены напишите /cancel.",
            )

        elif action == "rewrite_ai":
            post = get_post_or_raise(db, post_id)
            _set_pending(chat_id, "ai_rewrite", post.id)
            send_message(
                chat_id,
                f"🤖 ИИ-редактирование поста #{post.id}.\n\n"
                "Напишите, что нужно исправить. Например:\n"
                "<i>Сделай текст короче, убери повторы и добавь мягкий призыв записаться.</i>\n\n"
                "Картинка поста будет сохранена. Для отмены напишите /cancel.",
            )

        elif action == "plan_create":
            _clear_pending(chat_id)
            item, post = create_post_from_plan_item(db, post_id, with_image=False)
            send_message(chat_id, f"📝 По пункту плана #{item.id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)

        elif action == "plan_create_full":
            _clear_pending(chat_id)
            send_message(chat_id, f"Создаю пост и изображение по пункту плана #{post_id}.")
            item, post = create_post_from_plan_item(db, post_id, with_image=True)
            send_message(chat_id, f"📝🖼 По пункту плана #{item.id} создан пост #{post.id} с изображением.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        else:
            send_message(chat_id, "Неизвестное действие кнопки.")

    except (PostNotFoundError, PostStatusError, PlanItemNotFoundError, PlanItemStatusError, RuntimeError) as exc:
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

    if text and _handle_pending_text(chat_id, text, db):
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
            "/plan_week — создать 7 тем на неделю\n"
            "/plan — показать контент-план\n"
            "/create_from_plan ID — создать пост по пункту плана\n"
            "/create_full_from_plan ID — создать пост + картинку по пункту плана\n"
            "/cancel — отменить редактирование\n\n"
            "Редактирование, одобрение, отклонение, генерация изображения и публикация доступны кнопками под постом.",
        )
        return {"ok": True}

    if text == "/plan_week":
        send_message(chat_id, "Генерирую контент-план на неделю.")
        try:
            items = generate_week_plan(db, platform="telegram")
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации плана:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "<b>Контент-план на неделю создан:</b>")
        for item in items:
            send_message(chat_id, _plan_item_card(item), reply_markup=_plan_keyboard(item.id))
        return {"ok": True}

    if text == "/plan":
        items = list_plan_items(db, limit=20)
        if not items:
            send_message(chat_id, "Контент-план пока пуст. Создайте его командой /plan_week")
            return {"ok": True}

        send_message(chat_id, "<b>Текущий контент-план:</b>")
        for item in items:
            send_message(chat_id, _plan_item_card(item), reply_markup=_plan_keyboard(item.id))
        return {"ok": True}

    if text.startswith("/create_full_from_plan"):
        item_id, _ = _parse_id_and_payload(text, "/create_full_from_plan")
        if not item_id:
            send_message(chat_id, "Укажите ID пункта плана. Например: /create_full_from_plan 1")
            return {"ok": True}
        try:
            send_message(chat_id, f"Создаю пост и изображение по пункту плана #{item_id}.")
            item, post = create_post_from_plan_item(db, item_id, with_image=True)
            send_message(chat_id, f"По пункту плана #{item.id} создан пост #{post.id} с изображением.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/create_from_plan"):
        item_id, _ = _parse_id_and_payload(text, "/create_from_plan")
        if not item_id:
            send_message(chat_id, "Укажите ID пункта плана. Например: /create_from_plan 1")
            return {"ok": True}
        try:
            item, post = create_post_from_plan_item(db, item_id, with_image=False)
            send_message(chat_id, f"По пункту плана #{item.id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
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

    # Старые текстовые команды оставлены для совместимости.
    if text.startswith("/edit"):
        post_id, new_text = _parse_id_and_payload(text, "/edit")
        if not post_id or not new_text:
            send_message(chat_id, "Формат команды:\n/edit 1 Полный новый текст поста")
            return {"ok": True}
        try:
            post = edit_post_manually(db, post_id, new_text)
            send_message(chat_id, "Пост отредактирован вручную. Ранее созданная картинка сохранена.")
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
            send_message(chat_id, "Пост отредактирован через ИИ. Ранее созданная картинка сохранена.")
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
