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


HEADLINE_PROMPT = """
Ты — редактор заголовков для SMM медицинской клиники.

Сгенерируй короткий, емкий и естественный заголовок на русском языке.

Правила:
- 3–8 слов;
- без точки в конце;
- без кавычек;
- без кликбейта;
- без эмодзи;
- должен отражать тему поста;
- звучит как хороший заголовок для соцсетей клиники.

Верни только один заголовок, без пояснений.
""".strip()


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise RuntimeError(
            "OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables."
        )
    return OpenAI(api_key=settings.openai_api_key)


def generate_headline(topic: str, text: str | None = None) -> str:
    client = _get_client()
    user_prompt = f"""
Тема поста:
{topic}

Текст поста для контекста:
{(text or '')[:800]}
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": HEADLINE_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    headline = response.output_text.strip().strip('"«»')
    headline = headline.replace("\n", " ").strip()
    return headline[:120] or topic[:80]


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


CONTENT_PLAN_PROMPT = """
Ты — SMM-стратег медицинской клиники в Москве.

Составь контент-план на неделю для соцсетей клиники.

Правила:
- темы должны быть полезны потенциальным пациентам;
- без кликбейта и запугивания;
- учитывай сезонность, профилактику, анализы, УЗИ, терапию, вакцинацию, здоровье семьи;
- не обещай гарантированное лечение;
- темы должны подходить для Telegram-поста с возможностью изображения;
- верни строго 7 строк;
- каждая строка должна быть отдельной темой;
- без нумерации, без пояснений, без кавычек.
""".strip()


def generate_week_plan_topics(platform: str = "telegram") -> list[str]:
    client = _get_client()

    user_prompt = f"""
Платформа: {platform}

Сгенерируй 7 тем на ближайшую неделю.
""".strip()

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": CONTENT_PLAN_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw_lines = response.output_text.splitlines()
    topics: list[str] = []
    for line in raw_lines:
        cleaned = line.strip()
        cleaned = cleaned.lstrip("-•0123456789. )\t").strip()
        cleaned = cleaned.strip('"«»')
        if cleaned:
            topics.append(cleaned)

    # Страховка: максимум 7 тем, пустые строки отсекаем.
    return topics[:7]
