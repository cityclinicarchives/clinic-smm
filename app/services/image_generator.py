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
    base = f"""
Фотореалистичное изображение для SMM-поста современной частной медицинской клиники в Москве.

Тема поста: {post.title}

Контекст текста поста:
{(post.text or '')[:1200]}

Требования к изображению:
- современная чистая клиника, спокойная медицинская атмосфера;
- естественный дневной или мягкий студийный свет;
- люди выглядят как обычные жители Москвы;
- без узнаваемых реальных персон;
- без логотипов, брендов, текста, вывесок и водяных знаков;
- без пугающих медицинских сцен, крови, операций, инъекций крупным планом;
- без демонстрации конкретного диагноза;
- изображение должно выглядеть как качественная фотография для Instagram/Telegram;
- композиция подходит для публикации в соцсетях клиники.
""".strip()

    if custom_instruction:
        base += f"\n\nДополнительная инструкция: {custom_instruction.strip()}"

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
