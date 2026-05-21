import base64
import re
from pathlib import Path

from openai import OpenAI

from app.config import settings
from app.models import ContentPost
from app.services.visual_concept_engine import generate_visual_concept


class ImageGenerationError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ImageGenerationError(
            "OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables."
        )
    return OpenAI(api_key=settings.openai_api_key)


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-zа-яё0-9]+", "-", value, flags=re.IGNORECASE)
    value = value.strip("-")
    return value[:60] or "post"


def build_image_prompt(post: ContentPost, custom_instruction: str | None = None) -> str:
    """
    v19: картинка генерируется через Creative Visual Engine.
    Сначала GPT придумывает визуальную концепцию: эмоцию, метафору, сцену,
    формат и композицию. Потом эта концепция превращается в промпт для Images API.
    """
    headline = (post.headline or post.title or "").strip()
    concept = generate_visual_concept(post, custom_instruction)

    base = f"""
Создай готовую квадратную SMM-картинку 1024x1024 для Telegram/Instagram медицинской клиники в Москве.

Тема поста: {post.title}

Короткий заголовок, который нужно встроить в изображение ТОЧНО этим текстом:
«{headline}»

{concept.to_prompt_block()}

Фрагмент поста для понимания смысла:
{(post.text or '')[:1000]}

Ключевая задача:
Сделай НЕ банальную медицинскую stock-photo картинку.
Картинка должна иллюстрировать идею поста через визуальную метафору, предметную сцену, чек-лист, юмор или инфографику — в зависимости от визуальной концепции выше.

Строгие запреты:
- НЕ повторяй каждый раз шаблон "врач и пациент сидят в кабинете";
- НЕ делай одинаковые сцены консультации для разных тем;
- НЕ добавляй логотипы, водяные знаки, названия брендов;
- НЕ добавляй лишний русский текст кроме указанного заголовка;
- НЕ обрезай заголовок;
- НЕ превращай русский текст в нечитаемые символы;
- НЕ используй пугающие медицинские сцены, кровь, операции, инъекции крупным планом.

Требования к заголовку внутри изображения:
- заголовок должен быть крупным, контрастным, читабельным;
- заголовок должен полностью помещаться в изображение;
- если заголовок длинный, перенеси его на 2–3 строки, но не обрезай;
- используй красивый современный блок: скругленная карточка, плашка, clean medical design;
- русский текст должен быть написан правильно.

Стиль:
- современный качественный SMM-дизайн;
- clean healthcare advertising;
- можно использовать мягкий юмор, если концепция это допускает;
- люди, если есть, выглядят как обычные жители Москвы;
- изображение должно быть разным от поста к посту и точно связано с темой.
""".strip()

    return base


def generate_image_for_post(post: ContentPost, custom_instruction: str | None = None) -> tuple[str, str]:
    """
    Генерирует изображение для поста и сохраняет его локально.
    Возвращает: (image_path, image_prompt)
    """
    client = _get_client()
    prompt = build_image_prompt(post, custom_instruction)

    try:
        response = client.images.generate(
            model=settings.openai_image_model,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
    except Exception as exc:
        raise ImageGenerationError(f"Ошибка генерации изображения: {exc}") from exc

    image_data = response.data[0]
    b64_json = getattr(image_data, "b64_json", None)
    if not b64_json:
        raise ImageGenerationError(
            "OpenAI Images API не вернул b64_json. Проверьте модель OPENAI_IMAGE_MODEL."
        )

    image_bytes = base64.b64decode(b64_json)

    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    filename = f"post-{post.id}-{_slugify(post.title)}.png"
    path = images_dir / filename
    path.write_bytes(image_bytes)

    return str(path), prompt
