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


def generate_post_text(topic: str, platform: str, service_offer: str | None = None) -> str:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise RuntimeError(
            "OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables."
        )

    client = OpenAI(api_key=settings.openai_api_key)

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
