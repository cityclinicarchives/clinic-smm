from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ContentPost
from app.services.copywriter import generate_post_text
from app.services.telegram_bot import get_webhook_info, send_message, set_webhook

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _is_admin(chat_id: int | str) -> bool:
    if not settings.admin_telegram_id:
        return True
    return str(chat_id) == str(settings.admin_telegram_id)


def _shorten(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n...текст обрезан, полный пост сохранен в базе."


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

    if text == "/start":
        send_message(
            chat_id,
            "Бот SMM-менеджера работает.\n\n"
            "Команды:\n"
            "/generate тема поста — создать пост\n"
            "/posts — показать последние посты\n"
            "/help — помощь",
        )
        return {"ok": True}

    if text == "/help":
        send_message(
            chat_id,
            "Пример:\n"
            "/generate Как понять, что организму не хватает витамина D\n\n"
            "Бот создаст пост для Telegram, сохранит его в базе и пришлет текст сюда.",
        )
        return {"ok": True}

    if text == "/posts":
        posts = db.query(ContentPost).order_by(ContentPost.id.desc()).limit(5).all()
        if not posts:
            send_message(chat_id, "Постов пока нет.")
            return {"ok": True}

        lines = ["Последние посты:"]
        for post in posts:
            lines.append(f"#{post.id} | {post.platform} | {post.status} | {post.title}")
        send_message(chat_id, "\n".join(lines))
        return {"ok": True}

    if text.startswith("/generate"):
        topic = text.replace("/generate", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост, подождите 20–60 секунд...")

        try:
            generated_text = generate_post_text(
                topic=topic,
                platform="telegram",
                service_offer=None,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{exc}")
            return {"ok": True}

        post = ContentPost(
            title=topic,
            platform="telegram",
            text=generated_text,
            status="generated",
            ai_model=settings.openai_model,
        )
        db.add(post)
        db.commit()
        db.refresh(post)

        send_message(
            chat_id,
            f"Пост создан и сохранен в базе. ID: {post.id}\n\n{_shorten(generated_text)}",
        )
        return {"ok": True}

    send_message(
        chat_id,
        "Я пока понимаю только команды /start, /generate и /posts."
    )
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
