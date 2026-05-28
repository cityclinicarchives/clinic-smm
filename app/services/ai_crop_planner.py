import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image

from app.config import settings


class CropPlannerError(RuntimeError):
    pass


def _image_data_url(image: Image.Image) -> str:
    buf = BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{data}"


def _safe_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def plan_crops_with_ai(
    *,
    client: OpenAI,
    model: str,
    source_image: Image.Image,
    cards: list[dict[str, Any]],
    contract_summary_text: str = "",
) -> dict[str, Any]:
    """Ask vision model for exact crop plan for visual units.

    This step replaces direct use of source_bbox from reconstruction blueprint.
    It returns single- or multi-part crop plan with normalized coordinates.
    """
    units = []
    for idx, card in enumerate(cards, start=1):
        units.append({
            "unit_id": card.get("id") or f"unit_{idx}",
            "title": card.get("title") or f"Блок {idx}",
            "type": card.get("type"),
            "source_policy": card.get("source_policy"),
            "old_element": card.get("old_element"),
            "new_element": card.get("new_element"),
            "visual_element": card.get("visual_element"),
            "replacement_prompt": card.get("replacement_prompt"),
            "source_location_hint": card.get("source_location_hint"),
        })

    prompt = f"""
Ты — AI Crop Planner для медицинских инфографик.

Твоя задача — НЕ писать пост и НЕ проектировать дизайн. Твоя единственная задача: точно определить, какие области исходного изображения нужно вырезать как визуальные материалы для новых карточек.

Входные элементы final_units:
{json.dumps(units, ensure_ascii=False, indent=2)}

Контракт реконструкции:
{contract_summary_text}

Правила:
1. Не вырезай всю карточку целиком, если нужны только визуальные элементы.
2. Не включай старые подписи, желтые лейблы, синий фон, watermark, username, интерфейс соцсети, лайки, кнопки, соседние элементы.
3. Если укус/симптом и насекомое/объект разделены или рядом есть подпись — используй multi_part_bbox.
4. Если элемент заменяется по replacement_rules или source_policy="replace_with_new" — не вырезай старый элемент, верни raw_crop_strategy="impossible_use_generate_new".
5. Если невозможно аккуратно вырезать полезный элемент — лучше укажи impossible_use_generate_new, чем плохой bbox.
6. Координаты bbox должны быть максимально плотными вокруг полезного визуала.
7. Координаты bbox строго нормализованы 0..1 относительно исходного изображения.
8. Для карточек типа "укус + насекомое" обычно лучше сделать 2 части:
   - bite_mark / skin_reaction
   - insect_or_object
9. Каждый bbox должен содержать только то, что нужно сохранить. Не захватывай чужие подписи и соседние карточки.
10. Верни только валидный JSON без пояснений.

Формат ответа:
{{
  "crop_plan": [
    {{
      "unit_id": "...",
      "title": "...",
      "source_policy": "use_reference_and_clean|replace_with_new|generate_new",
      "raw_crop_strategy": "single_bbox|multi_part_bbox|impossible_use_generate_new",
      "crop_parts": [
        {{
          "part": "bite_mark|insect|object|symptom",
          "bbox": {{"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1}},
          "keep": ["..."],
          "remove": ["старые подписи", "фон", "watermark", "social_ui"]
        }}
      ],
      "combine_mode": "compose_clean_visual",
      "quality_risk": "low|medium|high",
      "notes": "..."
    }}
  ]
}}
""".strip()

    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "Ты точный визуальный crop-planner. Отвечай только JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": _image_data_url(source_image)},
                ],
            },
        ],
    )
    return _safe_json(response.output_text)


def _bbox_ok(bbox: Any) -> bool:
    if not isinstance(bbox, dict):
        return False
    try:
        x = float(bbox.get("x", 0)); y = float(bbox.get("y", 0)); w = float(bbox.get("w", 0)); h = float(bbox.get("h", 0))
    except Exception:
        return False
    if x < -0.02 or y < -0.02 or x + w > 1.02 or y + h > 1.02:
        return False
    if w <= 0.015 or h <= 0.015:
        return False
    # reject broad regions that likely include whole source cards/screenshots
    if w > 0.38 or h > 0.38 or w * h > 0.12:
        return False
    return True


def validate_crop_plan(crop_plan: dict[str, Any], cards: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    items = crop_plan.get("crop_plan") if isinstance(crop_plan, dict) else None
    if not isinstance(items, list):
        return ["crop_plan_missing"]
    by_id = {str(x.get("unit_id") or ""): x for x in items if isinstance(x, dict)}
    for idx, card in enumerate(cards, start=1):
        uid = str(card.get("id") or f"unit_{idx}")
        policy = str(card.get("source_policy") or "").lower()
        item = by_id.get(uid)
        if not item:
            # tolerate title lookup later, but report
            issues.append(f"crop_plan_missing_unit:{uid}")
            continue
        strategy = str(item.get("raw_crop_strategy") or "").lower()
        if policy == "replace_with_new" or strategy == "impossible_use_generate_new":
            continue
        parts = item.get("crop_parts")
        if not isinstance(parts, list) or not parts:
            issues.append(f"crop_plan_no_parts:{uid}")
            continue
        for pidx, part in enumerate(parts, start=1):
            if not _bbox_ok(part.get("bbox")):
                issues.append(f"crop_plan_bad_bbox:{uid}:part_{pidx}")
    return issues


def repair_crop_plan_with_ai(
    *,
    client: OpenAI,
    model: str,
    source_image: Image.Image,
    cards: list[dict[str, Any]],
    crop_plan: dict[str, Any],
    issues: list[str],
    contract_summary_text: str = "",
) -> dict[str, Any]:
    prompt = f"""
Исправь crop_plan. Предыдущий план не прошел проверку.

Ошибки:
{json.dumps(issues, ensure_ascii=False, indent=2)}

Предыдущий crop_plan:
{json.dumps(crop_plan, ensure_ascii=False, indent=2)[:12000]}

Правила исправления:
- bbox должны быть плотными вокруг полезного визуала;
- не захватывай целые карточки, подписи, фон, UI, watermark;
- если одним bbox нельзя — используй multi_part_bbox;
- если элемент заменяемый — impossible_use_generate_new;
- верни только JSON в формате {{"crop_plan": [...]}}.

Контракт:
{contract_summary_text}
""".strip()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "Ты исправляешь только JSON crop_plan. Отвечай только JSON."},
            {"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": _image_data_url(source_image)},
            ]},
        ],
    )
    return _safe_json(response.output_text)
