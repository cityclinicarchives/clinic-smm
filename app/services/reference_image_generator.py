import base64
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from openai import OpenAI

from app.config import settings
from app.models import ContentAsset, ContentPost, ContentReconstruction
from app.services.telegram_bot import download_file_bytes


class ReferenceImageGenerationError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ReferenceImageGenerationError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-zа-яё0-9]+", "-", value, flags=re.IGNORECASE)
    value = value.strip("-")
    return value[:60] or "reconstruction"


def _load_spec(reconstruction: ContentReconstruction) -> dict[str, Any]:
    try:
        return json.loads(reconstruction.reconstruction_spec or "{}")
    except Exception:
        return {}


def has_reference_image(asset: ContentAsset | None) -> bool:
    return bool(asset and asset.media_file_id and asset.media_type in {"photo", "document"})


def build_reference_edit_prompt(reconstruction: ContentReconstruction, post: ContentPost, asset: ContentAsset | None) -> str:
    spec = _load_spec(reconstruction)
    visual = spec.get("visual") or {}
    structure = spec.get("structure") or {}
    title = spec.get("title") or {}
    audit = spec.get("medical_audit") or {}

    prompt_from_spec = visual.get("reference_edit_prompt") or visual.get("fallback_ai_image_prompt") or reconstruction.image_prompt or ""
    headline = post.headline or title.get("final") or reconstruction.final_title or post.title

    return f"""
Используй загруженную исходную картинку как главный визуальный референс.

Задача: создать новую улучшенную SMM-картинку/инфографику для медицинской клиники в Москве.

КРИТИЧЕСКИ ВАЖНО:
- НЕ создавай случайную новую картинку с нуля, если исходная структура хорошая.
- Сохрани сильную механику исходника: композицию, сетку, сравнение, логику чтения, визуальный hook.
- Не копируй чужой бренд, watermark, username, элементы интерфейса соцсети.
- Улучши дизайн: clean medical design, светлый фон, аккуратные блоки, читаемость.
- Исправь медицинские ошибки и убери категоричность.
- Русский текст должен быть коротким, крупным, без ошибок и полностью читаемым.
- Если сомневаешься в точности мелкого текста, лучше используй меньше текста и больше понятных визуальных блоков.

Итоговый заголовок, который должен быть на изображении:
«{headline}»

Blueprint title:
{json.dumps(title, ensure_ascii=False)}

Медицинский аудит:
{json.dumps(audit, ensure_ascii=False)[:3000]}

Структура/блоки:
{json.dumps(structure, ensure_ascii=False)[:5000]}

Визуальная стратегия:
{visual.get('strategy') or reconstruction.visual_strategy or ''}

Must include:
{json.dumps(visual.get('must_include') or [], ensure_ascii=False)}

Must avoid:
{json.dumps(visual.get('must_avoid') or [], ensure_ascii=False)}

Промпт из reconstruction spec:
{prompt_from_spec}

Финальное требование:
Сделай изображение как качественную медицинскую инфографику или SMM-карточку, которая выглядит лучше исходника, но узнаваемо сохраняет его сильную структуру и идею.
""".strip()


def generate_reference_reconstruction_image(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> tuple[str, str]:
    if not has_reference_image(asset):
        raise ReferenceImageGenerationError("У исходника нет изображения-референса.")

    client = _get_client()
    prompt = build_reference_edit_prompt(reconstruction, post, asset)
    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]

    # OpenAI image edit API обычно принимает file-like object. Сохраняем временный файл.
    suffix = ".jpg"
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)

    try:
        with tmp_path.open("rb") as image_file:
            response = client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
                size="1024x1024",
                n=1,
            )
    except Exception as exc:
        raise ReferenceImageGenerationError(f"Ошибка reference-based генерации изображения: {exc}") from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    image_data = response.data[0]
    b64_json = getattr(image_data, "b64_json", None)
    if not b64_json:
        raise ReferenceImageGenerationError("OpenAI Images API не вернул b64_json для reference edit.")

    output_bytes = base64.b64decode(b64_json)
    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"reconstruction-{reconstruction.id}-post-{post.id}-{_slugify(post.title)}.png"
    path = images_dir / filename
    path.write_bytes(output_bytes)
    return str(path), prompt
