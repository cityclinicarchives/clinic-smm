from pathlib import Path

import httpx

from app.config import settings


class TelegramBotError(RuntimeError):
    pass


def _check_token() -> None:
    if not settings.telegram_bot_token:
        raise TelegramBotError(
            "TELEGRAM_BOT_TOKEN не задан. Добавьте TELEGRAM_BOT_TOKEN в Railway Variables."
        )


def send_message(chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=payload)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram sendMessage error: {response.text}")


def send_photo(chat_id: int | str, photo_path: str, caption: str | None = None, reply_markup: dict | None = None) -> None:
    _check_token()

    path = Path(photo_path)
    if not path.exists():
        raise TelegramBotError(f"Файл изображения не найден: {photo_path}")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    data = {
        "chat_id": str(chat_id),
        "parse_mode": "HTML",
    }
    if caption:
        # Лимит caption в Telegram — 1024 символа. Полный текст при необходимости отправим отдельно.
        data["caption"] = caption[:1024]
    if reply_markup:
        import json
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    with path.open("rb") as file_obj:
        files = {"photo": (path.name, file_obj, "image/png")}
        with httpx.Client(timeout=60) as client:
            response = client.post(url, data=data, files=files)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram sendPhoto error: {response.text}")


def send_document(chat_id: int | str, document_path: str, caption: str | None = None) -> None:
    _check_token()

    path = Path(document_path)
    if not path.exists() or not path.is_file():
        raise TelegramBotError(f"Файл документа не найден: {document_path}")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    data = {
        "chat_id": str(chat_id),
        "parse_mode": "HTML",
    }
    if caption:
        data["caption"] = caption[:1024]

    with path.open("rb") as file_obj:
        files = {"document": (path.name, file_obj, "application/json")}
        with httpx.Client(timeout=60) as client:
            response = client.post(url, data=data, files=files)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram sendDocument error: {response.text}")


def set_webhook(webhook_url: str) -> dict:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
    }

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=payload)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram setWebhook error: {response.text}")

    return response.json()


def get_webhook_info() -> dict:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"

    with httpx.Client(timeout=30) as client:
        response = client.get(url)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram getWebhookInfo error: {response.text}")

    return response.json()


def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=payload)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram answerCallbackQuery error: {response.text}")


def get_file_path(file_id: str) -> str:
    _check_token()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile"
    with httpx.Client(timeout=30) as client:
        response = client.post(url, json={"file_id": file_id})
    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram getFile error: {response.text}")
    data = response.json()
    if not data.get("ok") or not data.get("result", {}).get("file_path"):
        raise TelegramBotError(f"Telegram getFile unexpected response: {data}")
    return data["result"]["file_path"]


def download_file_bytes(file_id: str) -> bytes:
    _check_token()
    file_path = get_file_path(file_id)
    url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
    with httpx.Client(timeout=60) as client:
        response = client.get(url)
    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram file download error: {response.text}")
    return response.content
