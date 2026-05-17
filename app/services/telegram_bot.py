import httpx

from app.config import settings


class TelegramBotError(RuntimeError):
    pass


def _check_token() -> None:
    if not settings.telegram_bot_token:
        raise TelegramBotError(
            "TELEGRAM_BOT_TOKEN не задан. Добавьте TELEGRAM_BOT_TOKEN в Railway Variables."
        )


def send_message(chat_id: int | str, text: str) -> None:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=payload)

    if response.status_code >= 400:
        raise TelegramBotError(f"Telegram sendMessage error: {response.text}")


def set_webhook(webhook_url: str) -> dict:
    _check_token()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message"],
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
