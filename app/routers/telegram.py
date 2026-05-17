import html

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.post_manager import (
    PostNotFoundError,
    approve_post,
    create_generated_post,
    edit_post_manually,
    get_post_or_raise,
    list_recent_posts,
    reject_post,
    rewrite_post_with_ai,
)
from app.services.telegram_bot import get_webhook_info, send_message, set_webhook

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


def _post_card(post, include_text: bool = False) -> str:
    lines = [
        f"<b>Пост #{post.id}</b>",
        f"Тема: {_safe(post.title)}",
        f"Платформа: {_safe(post.platform)}",
        f"Статус: {_safe(post.status)}",
    ]
    if include_text:
        lines.append("")
        lines.append(_safe(_shorten(post.text or "Текст пустой.")))
        lines.append("")
        lines.append(f"/approve {post.id} — одобрить")
        lines.append(f"/reject {post.id} — отклонить")
        lines.append(f"/rewrite {post.id} инструкция — отредактировать через ИИ")
        lines.append(f"/edit {post.id} новый текст — заменить вручную")
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


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()
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
            "/generate тема — создать пост\n"
            "/posts — последние посты\n"
            "/post ID — посмотреть пост\n"
            "/approve ID — одобрить\n"
            "/reject ID — отклонить\n"
            "/edit ID новый текст — заменить текст вручную\n"
            "/rewrite ID инструкция — отредактировать через ИИ\n\n"
            "Пример:\n"
            "/rewrite 1 Сделай короче и убери повторы",
        )
        return {"ok": True}

    if text == "/posts":
        posts = list_recent_posts(db, limit=10)
        if not posts:
            send_message(chat_id, "Постов пока нет.")
            return {"ok": True}

        lines = ["<b>Последние посты:</b>"]
        for post in posts:
            lines.append(f"#{post.id} | {_safe(post.platform)} | {_safe(post.status)} | {_safe(post.title)}")
        lines.append("\nЧтобы открыть пост: /post ID")
        send_message(chat_id, "\n".join(lines))
        return {"ok": True}

    if text.startswith("/post"):
        post_id, _ = _parse_id_and_payload(text, "/post")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /post 1")
            return {"ok": True}
        try:
            post = get_post_or_raise(db, post_id)
            send_message(chat_id, _post_card(post, include_text=True))
        except PostNotFoundError as exc:
            send_message(chat_id, str(exc))
        return {"ok": True}

    if text.startswith("/generate"):
        topic = text.replace("/generate", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост, подождите 20–60 секунд...")

        try:
            post = create_generated_post(
                db=db,
                topic=topic,
                platform="telegram",
                service_offer=None,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "Пост создан и сохранен в базе.\n\n" + _post_card(post, include_text=True))
        return {"ok": True}

    if text.startswith("/approve"):
        post_id, _ = _parse_id_and_payload(text, "/approve")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /approve 1")
            return {"ok": True}
        try:
            post = approve_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} одобрен. Статус: approved")
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/reject"):
        post_id, _ = _parse_id_and_payload(text, "/reject")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /reject 1")
            return {"ok": True}
        try:
            post = reject_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} отклонен. Статус: rejected")
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
            send_message(chat_id, "Пост отредактирован вручную.\n\n" + _post_card(post, include_text=True))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/rewrite"):
        post_id, instruction = _parse_id_and_payload(text, "/rewrite")
        if not post_id or not instruction:
            send_message(chat_id, "Формат команды:\n/rewrite 1 Сделай текст короче и убери повторы")
            return {"ok": True}

        send_message(chat_id, "Редактирую пост через ИИ, подождите 20–60 секунд...")
        try:
            post = rewrite_post_with_ai(db, post_id, instruction)
            send_message(chat_id, "Пост отредактирован через ИИ.\n\n" + _post_card(post, include_text=True))
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
