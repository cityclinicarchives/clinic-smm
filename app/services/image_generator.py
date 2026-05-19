import base64
import re
from pathlib import Path

from openai import OpenAI

from app.config import settings
from app.models import ContentPost


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
    Создает промпт для OpenAI Images API.

    ВАЖНО: начиная с v13 заголовок больше НЕ накладывается сервером через Pillow.
    Картинка сразу генерируется как готовая SMM-карточка с дизайнерским блоком
    заголовка внутри изображения. Это убирает проблему мелкого/битого текста,
    который раньше дорисовывался на Railway.
    """
    headline = (post.headline or post.title or "").strip()

    base = f"""
Создай готовую квадратную SMM-картинку 1024x1024 для Telegram/Instagram медицинской клиники в Москве.

Тема поста: {post.title}

Короткий заголовок, который нужно встроить в изображение ТОЧНО этим текстом:
«{headline}»

Контекст поста:
{(post.text or '')[:1000]}

Архитектура изображения:
- верхняя часть: фотореалистичная сцена в современной частной медицинской клинике;
- нижняя часть: красивый дизайнерский блок/карточка с заголовком;
- заголовок должен быть крупным, контрастным, читабельным, не мелким;
- заголовок должен полностью помещаться в изображение;
- если заголовок длинный, перенеси его на 2 строки, но НЕ обрезай;
- используй современный медицинский дизайн: белый фон, темно-синий/графитовый текст, мягкие зеленые или синие акценты, скругленные блоки;
- стиль как у качественного поста клиники в Telegram/Instagram, не как скриншот и не как мем.

Требования к кириллице:
- русский текст должен быть написан правильно;
- не искажать буквы;
- не превращать текст в набор символов;
- не добавлять другие слова, подписи, логотипы, водяные знаки или вывески.

Требования к медицинской сцене:
- современная чистая клиника, спокойная медицинская атмосфера;
- естественный дневной или мягкий студийный свет;
- люди выглядят как обычные жители Москвы;
- без узнаваемых реальных персон;
- без пугающих сцен, крови, операций, инъекций крупным планом;
- без демонстрации конкретного диагноза;
- композиция должна оставлять место под дизайнерский заголовок внутри картинки.
""".strip()

    if custom_instruction:
        base += f"\n\nДополнительная инструкция пользователя: {custom_instruction.strip()}"

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
