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


class ComponentInfographicError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ComponentInfographicError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-zа-яё0-9]+", "-", value, flags=re.IGNORECASE)
    value = value.strip("-")
    return value[:60] or "component-infographic"


def _load_spec(reconstruction: ContentReconstruction) -> dict[str, Any]:
    try:
        return json.loads(reconstruction.reconstruction_spec or "{}")
    except Exception:
        return {}


def has_component_reference(asset: ContentAsset | None) -> bool:
    return bool(asset and asset.media_file_id and asset.media_type in {"photo", "document"})


def _compact_blocks(blocks: Any, limit: int = 18) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    result: list[dict[str, Any]] = []
    for block in blocks[:limit]:
        if not isinstance(block, dict):
            continue
        result.append({
            "id": block.get("id"),
            "type": block.get("type"),
            "title": block.get("title"),
            "lines": block.get("lines"),
            "visual_element": block.get("visual_element"),
            "source_policy": block.get("source_policy"),
            "source_location_hint": block.get("source_location_hint"),
            "change_reason": block.get("change_reason"),
        })
    return result


def build_component_infographic_prompt(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> str:
    spec = _load_spec(reconstruction)
    title = spec.get("title") or {}
    structure = spec.get("structure") or {}
    visual = spec.get("visual") or {}
    audit = spec.get("medical_audit") or {}
    source_analysis = spec.get("source_analysis") or {}

    headline = post.headline or title.get("final") or reconstruction.final_title or post.title
    blocks = _compact_blocks(structure.get("blocks"))

    return f"""
Ты работаешь как component-based infographic engine и арт-директор медицинской клиники.

У тебя есть исходная картинка-референс. Используй ее НЕ как случайное вдохновение, а как визуальный материал для реконструкции.

ЗАДАЧА:
Создать новую оригинальную медицинскую инфографику, собранную из отдельных блоков/карточек, с сохранением сильных элементов исходника и точечными улучшениями.

ГЛАВНЫЙ ПРИНЦИП:
Сначала мысленно разбей инфографику на компоненты. Затем обработай каждый компонент отдельно. Затем собери их в единую аккуратную инфографику.

ИТОГОВЫЙ ЗАГОЛОВОК:
«{headline}»

КАК РАБОТАТЬ С РЕФЕРЕНСОМ:
- Для блоков с source_policy="preserve_from_reference" возьми визуальную идею/форму/пример из исходного изображения максимально близко, но без watermark, username, элементов интерфейса и чужого бренда.
- Для блоков с source_policy="use_reference_and_clean" сохрани смысл и внешний образ исходного элемента, но улучши качество, чистоту, цвет, выравнивание и стиль.
- Для блоков с source_policy="replace_with_new" НЕ копируй исходный элемент; создай новый элемент, но в том же визуальном стиле, масштабе и композиционной логике.
- Для блоков с source_policy="generate_new" создай новый компонент с нуля в стиле всей инфографики.

ОСОБО ВАЖНО ДЛЯ МЕДИЦИНСКИХ СРАВНИТЕЛЬНЫХ ИНФОГРАФИК:
- Не выдумывай точные диагнозы по внешнему виду.
- Избегай категоричных формулировок.
- Добавь короткий дисклеймер, если реакции/симптомы могут отличаться.
- Добавь warning signs и безопасные действия, если они указаны в blueprint.

ТИПОГРАФИКА:
- Русский текст допускается, но только крупный, короткий и четкий.
- Не более 1 заголовка + 1–2 коротких строк в одной карточке.
- Не делай длинных абзацев внутри карточек.
- Не используй мелкий шрифт.
- Не искажай кириллицу.
- Если текста слишком много, сократи его до коротких фраз.
- Инфографика должна читаться с телефона за 2–3 секунды.

ДИЗАЙН:
- clean medical minimal design;
- светлый фон;
- аккуратные карточки;
- темно-синий/графитовый основной текст;
- бирюзовые/зеленые акценты;
- мягкие скругления;
- одинаковые отступы;
- без агрессивного синего фона;
- без логотипов, watermark, username, интерфейса соцсетей.

SOURCE ANALYSIS:
{json.dumps(source_analysis, ensure_ascii=False)[:4000]}

MEDICAL AUDIT:
{json.dumps(audit, ensure_ascii=False)[:5000]}

COMPONENT BLUEPRINT:
{json.dumps(blocks, ensure_ascii=False, indent=2)[:10000]}

WARNING BLOCK:
{json.dumps(structure.get('warning_block') or audit.get('danger_signs') or [], ensure_ascii=False)[:2000]}

ACTION BLOCK:
{json.dumps(structure.get('action_block') or audit.get('safe_actions') or [], ensure_ascii=False)[:2000]}

FOOTER:
{structure.get('footer') or audit.get('disclaimer') or ''}

VISUAL STRATEGY:
{visual.get('strategy') or reconstruction.visual_strategy or ''}

COMPONENT PROMPT FROM SPEC:
{visual.get('component_generation_prompt') or ''}

MUST INCLUDE:
{json.dumps(visual.get('must_include') or [], ensure_ascii=False)[:3000]}

MUST AVOID:
{json.dumps(visual.get('must_avoid') or [], ensure_ascii=False)[:3000]}

ФИНАЛЬНАЯ ПРОВЕРКА ПЕРЕД ОТВЕТОМ:
- Все карточки должны быть выровнены.
- Русский текст должен быть читаемым.
- Визуальные элементы из исходника должны быть сохранены там, где source_policy требует сохранить.
- Замененные элементы должны выглядеть в том же стиле, что сохраненные.
- Инфографика должна выглядеть как профессиональная медицинская SMM-карточка.
""".strip()


def generate_component_infographic_image(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> tuple[str, str]:
    if not has_component_reference(asset):
        raise ComponentInfographicError("У исходника нет изображения-референса для component reconstruction.")

    client = _get_client()
    prompt = build_component_infographic_prompt(reconstruction, post, asset)
    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]

    with NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
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
        raise ComponentInfographicError(f"Ошибка component-based генерации инфографики: {exc}") from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    image_data = response.data[0]
    b64_json = getattr(image_data, "b64_json", None)
    if not b64_json:
        raise ComponentInfographicError("OpenAI Images API не вернул b64_json для component infographic.")

    output_bytes = base64.b64decode(b64_json)
    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"component-infographic-{reconstruction.id}-post-{post.id}-{_slugify(post.title)}.png"
    path = images_dir / filename
    path.write_bytes(output_bytes)
    return str(path), prompt
