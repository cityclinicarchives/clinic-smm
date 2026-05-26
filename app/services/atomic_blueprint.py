
import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image


VISUAL_CARD_TYPES = {"comparison_card", "card", "tile", "visual_card", "comparison_item"}
REFERENCE_POLICIES = {"preserve_from_reference", "use_reference_and_clean"}
GENERATE_POLICIES = {"replace_with_new", "generate_new"}


def _blocks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    structure = spec.setdefault("structure", {})
    blocks = structure.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    # v27 supports optional atomic_blueprint/content_units, but normalizes it back to structure.blocks.
    atomic = spec.get("atomic_blueprint") or {}
    units = atomic.get("content_units") if isinstance(atomic, dict) else None
    if isinstance(units, list) and units:
        # Prefer atomic content units when they are more detailed than old blocks.
        old_cards = [b for b in blocks if isinstance(b, dict) and str(b.get("type") or "").lower() in VISUAL_CARD_TYPES]
        new_cards = [u for u in units if isinstance(u, dict) and str(u.get("type") or "").lower() in VISUAL_CARD_TYPES]
        if len(new_cards) >= max(1, len(old_cards)):
            # Keep header/footer blocks from structure; replace only visual card-like items.
            non_cards = [b for b in blocks if isinstance(b, dict) and str(b.get("type") or "").lower() not in VISUAL_CARD_TYPES]
            blocks = non_cards + units
            structure["blocks"] = blocks
    return [b for b in blocks if isinstance(b, dict)]


def normalize_atomic_blocks(spec: dict[str, Any]) -> dict[str, Any]:
    blocks = _blocks(spec)
    structure = spec.setdefault("structure", {})
    for i, b in enumerate(blocks, 1):
        b.setdefault("id", f"block_{i}")
        t = str(b.get("type") or "").lower().strip()
        if t == "comparison_item":
            b["type"] = "comparison_card"
        if str(b.get("type") or "").lower() in VISUAL_CARD_TYPES:
            b.setdefault("number", len([x for x in blocks[:i] if str(x.get("type") or "").lower() in VISUAL_CARD_TYPES]))
            # All reference-driven cards need short text only.
            lines = b.get("lines")
            if isinstance(lines, str):
                b["lines"] = [lines]
            elif not isinstance(lines, list):
                b["lines"] = []
            b["lines"] = [str(x).strip() for x in b["lines"] if str(x).strip()][:2]
    structure["blocks"] = blocks
    structure["expected_block_count"] = len(blocks)
    return spec


def _bbox_area(bbox: Any) -> float | None:
    if not isinstance(bbox, dict):
        return None
    try:
        w = float(bbox.get("w", bbox.get("width")))
        h = float(bbox.get("h", bbox.get("height")))
    except Exception:
        return None
    if w > 1.5 or h > 1.5:
        # Pixel bbox: cannot know source dimensions here.
        return None
    return max(0.0, w) * max(0.0, h)


def _looks_grouped(block: dict[str, Any]) -> bool:
    text = " ".join(str(block.get(k) or "") for k in ["id", "title", "visual_element", "source_location_hint", "change_reason"])
    text = text.lower()
    grouped_words = ["вся сетка", "сетка", "таблица", "коллаж", "группа", "все укусы", "все карточки", "общий блок", "whole grid", "grid", "collage", "all cards"]
    return any(w in text for w in grouped_words)


def atomic_blueprint_issues(spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    blocks = _blocks(spec)
    if not blocks:
        return ["no_structure_blocks"]
    cards = [b for b in blocks if str(b.get("type") or "").lower() in VISUAL_CARD_TYPES]
    if not cards:
        issues.append("no_atomic_visual_cards")
    for idx, b in enumerate(cards, 1):
        policy = str(b.get("source_policy") or "").lower()
        if _looks_grouped(b):
            issues.append(f"card_{idx}_is_grouped_not_atomic")
        if policy in REFERENCE_POLICIES:
            bbox = b.get("source_bbox")
            if not isinstance(bbox, dict):
                issues.append(f"card_{idx}_reference_policy_without_bbox")
            else:
                area = _bbox_area(bbox)
                if area is not None and area > 0.22:
                    issues.append(f"card_{idx}_bbox_too_large_area_{area:.2f}")
                if area is not None and area < 0.005:
                    issues.append(f"card_{idx}_bbox_too_small_area_{area:.3f}")
        if policy in GENERATE_POLICIES and not (b.get("replacement_prompt") or b.get("visual_element") or b.get("new_element")):
            issues.append(f"card_{idx}_generated_without_visual_prompt")
        title = str(b.get("title") or "").strip()
        if not title:
            issues.append(f"card_{idx}_missing_title")
        # Dense medical visual cards should not have long paragraphs.
        for li, line in enumerate(b.get("lines") or [], 1):
            if len(str(line).split()) > 10:
                issues.append(f"card_{idx}_line_{li}_too_long")
    # If source analysis says 3x3 or 9 items, card count must reflect that.
    source_text = json.dumps(spec.get("source_analysis") or {}, ensure_ascii=False).lower()
    if ("3×3" in source_text or "3x3" in source_text or "9" in source_text and "карточ" in source_text) and len(cards) < 8:
        issues.append(f"source_suggests_9_cards_but_only_{len(cards)}")
    return issues


def _image_part_from_bytes(image_bytes: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"}


def repair_atomic_blueprint_with_ai(
    client: OpenAI,
    model: str,
    spec: dict[str, Any],
    image_bytes: bytes | None,
    issues: list[str],
    extra_context: str = "",
) -> dict[str, Any]:
    """Ask the model to repair only the blueprint, not rewrite the whole task.

    This is intentionally called only when the first blueprint is not atomic enough.
    """
    system = """
Ты — строгий валидатор и архитектор component-based medical infographic blueprint.
Твоя задача — исправить JSON blueprint так, чтобы программа могла ФИЗИЧЕСКИ вырезать отдельные элементы из исходной картинки и собрать новую инфографику.
Не пиши markdown. Верни только валидный JSON.
""".strip()
    user = f"""
Исправь blueprint. Текущие ошибки валидатора:
{json.dumps(issues, ensure_ascii=False, indent=2)}

КРИТИЧЕСКИЕ ПРАВИЛА v27:
1. Каждый визуальный элемент должен быть атомарным блоком. Нельзя делать один блок «вся сетка», «таблица», «коллаж», «все укусы».
2. Если исходник содержит сетку элементов, каждый элемент сетки должен стать отдельным comparison_card.
3. Для каждого сохраняемого элемента source_policy="preserve_from_reference" или "use_reference_and_clean" и ОБЯЗАТЕЛЬНО source_bbox.
4. source_bbox должен охватывать только конкретный полезный визуал/карточку, не весь коллаж, не интерфейс соцсети, не watermark.
5. Если элемент нужно заменить под локальный контекст, укажи source_policy="replace_with_new", old_element, new_element, replacement_prompt, change_reason.
6. Нельзя сокращать количество важных элементов из-за нехватки места. Для плотной инфографики выбирай canvas.aspect_ratio 4:5, 3:4 или 2:3.
7. Для каждой карточки: title короткий, lines максимум 1–2 короткие строки, visual_element конкретный.
8. structure.expected_block_count должен равняться len(structure.blocks).
9. layout может быть приблизительным, но должен быть без явных пересечений в координатах 0..1.
10. Верни полностью исправленный JSON, сохранив medical_audit, post, title, visual.

Дополнительный контекст:
{extra_context[:3000]}

Текущий JSON:
{json.dumps(spec, ensure_ascii=False, indent=2)[:30000]}
""".strip()
    content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
    if image_bytes:
        content.append(_image_part_from_bytes(image_bytes))
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    )
    raw = response.output_text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    repaired = json.loads(cleaned)
    return normalize_atomic_blocks(repaired)
