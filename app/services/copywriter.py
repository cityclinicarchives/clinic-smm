from openai import OpenAI

from app.config import settings


SYSTEM_PROMPT = """
Ты — профессиональный SMM-копирайтер медицинской клиники в Москве.

Задача: создавать полезные, спокойные и понятные публикации для пациентов.

Правила:
- не обещай гарантированное лечение;
- не ставь диагнозы по симптомам;
- не призывай к самолечению;
- не используй агрессивный кликбейт;
- не пиши от лица конкретного врача, если пользователь это не попросил;
- пиши естественно, без канцелярита и без типичного ИИ-стиля;
- добавляй мягкий призыв записаться в клинику, если это уместно;
- для медицинских тем добавляй короткий дисклеймер: информация не заменяет консультацию врача.

Стиль:
- экспертный;
- человеческий;
- доброжелательный;
- без чрезмерного количества эмодзи.
""".strip()


REWRITE_PROMPT = """
Ты — редактор SMM-постов медицинской клиники.

Твоя задача — аккуратно отредактировать уже созданный пост по инструкции пользователя.

Правила:
- сохраняй медицинскую корректность;
- не добавляй обещания гарантированного результата;
- не ставь диагнозы;
- не делай текст агрессивным или пугающим;
- если пользователь просит сократить — сокращай без потери смысла;
- если пользователь просит исправить ошибку — исправляй только нужное;
- верни только готовый текст поста, без комментариев от себя.
""".strip()


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise RuntimeError(
            "OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables."
        )
    return OpenAI(api_key=settings.openai_api_key)


def generate_post_text(topic: str, platform: str, service_offer: str | None = None) -> str:
    client = _get_client()

    offer_block = ""
    if service_offer:
        offer_block = f"\nКоммерческое предложение клиники, если уместно встроить в текст:\n{service_offer}\n"

    user_prompt = f"""
Напиши пост для соцсети медицинской клиники.

Тема:
{topic}

Платформа:
{platform}
{offer_block}
Требования:
- 1200–2000 символов;
- сильное, но не кликбейтное начало;
- польза для пациента;
- простая структура;
- без медицинских обещаний;
- без давления и запугивания;
- в конце мягкий CTA на запись, если это уместно.
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    return response.output_text


def rewrite_post_text(original_text: str, instruction: str, platform: str = "telegram") -> str:
    client = _get_client()

    user_prompt = f"""
Платформа: {platform}

Инструкция редактора:
{instruction}

Исходный пост:
{original_text}
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    return response.output_text
