import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image

from app.prompts.visual_decomposition import (
    VISUAL_DECOMPOSITION_SYSTEM_PROMPT,
    VISUAL_DECOMPOSITION_USER_TEMPLATE,
)
from app.services.medical_semantic_segmentation import (
    component_role,
    component_sort_key,
    medical_semantic_issues,
    normalize_component_map,
)


def _image_to_data_url(image: Image.Image) -> str:
    buf = BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _safe_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def build_visual_decomposition(
    *,
    client: OpenAI,
    model: str,
    source_image: Image.Image,
    cards: list[dict[str, Any]],
    contract_summary_text: str,
) -> dict[str, Any]:
    cards_for_prompt = []
    for i, card in enumerate(cards, start=1):
        cards_for_prompt.append({
            "idx": i,
            "id": card.get("id") or card.get("unit_id") or f"unit_{i}",
            "title": card.get("title") or card.get("new_element") or card.get("visual_element") or f"Unit {i}",
            "source_policy": card.get("source_policy"),
            "old_element": card.get("old_element"),
            "new_element": card.get("new_element"),
            "visual_element": card.get("visual_element"),
            "replacement_prompt": card.get("replacement_prompt"),
            "lines": card.get("lines"),
        })
    prompt = VISUAL_DECOMPOSITION_USER_TEMPLATE.format(
        cards_json=json.dumps(cards_for_prompt, ensure_ascii=False, indent=2),
        contract_summary=contract_summary_text,
    )
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": VISUAL_DECOMPOSITION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": _image_to_data_url(source_image)},
                ],
            },
        ],
    )
    return normalize_component_map(_safe_json(response.output_text))


def _norm_title(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _card_keys(card: dict[str, Any], idx: int) -> set[str]:
    keys = {f"unit_{idx}", str(idx)}
    for field in ("id", "unit_id", "title", "visual_element", "new_element", "old_element"):
        v = card.get(field)
        if v:
            keys.add(_norm_title(v))
    return {k for k in keys if k}


def _unit_keys(unit: dict[str, Any]) -> set[str]:
    keys = set()
    for field in ("unit_id", "id", "title", "source_title"):
        v = unit.get(field)
        if v:
            keys.add(_norm_title(v))
    return {k for k in keys if k}


def validate_component_map(component_map: dict[str, Any], cards: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    units = component_map.get("units")
    if not isinstance(units, list) or not units:
        return ["visual_decomposition_no_units"]
    issues.extend(medical_semantic_issues(component_map))
    for idx, card in enumerate(cards, start=1):
        policy = str(card.get("source_policy") or "").lower()
        if policy in {"replace_with_new", "generate_new"}:
            continue
        keys = _card_keys(card, idx)
        matched = None
        for unit in units:
            if isinstance(unit, dict) and keys & _unit_keys(unit):
                matched = unit
                break
        if matched is None:
            issues.append(f"visual_decomposition_missing_unit:{card.get('title') or idx}")
            continue
        preserved = []
        has_primary = False
        for comp in matched.get("components") or []:
            if not isinstance(comp, dict):
                continue
            if str(comp.get("action") or "").lower() == "preserve":
                bbox = comp.get("bbox")
                if isinstance(bbox, dict):
                    try:
                        x = float(bbox.get("x", 0)); y = float(bbox.get("y", 0)); w = float(bbox.get("w", 0)); h = float(bbox.get("h", 0))
                        if 0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1:
                            # reject huge entire-card bboxes as not decomposed
                            if w * h < 0.20:
                                preserved.append(comp)
                                if component_role(comp) == "primary":
                                    has_primary = True
                    except Exception:
                        pass
        if not preserved:
            issues.append(f"visual_decomposition_no_preserved_visual_components:{card.get('title') or idx}")
        if preserved and not has_primary:
            issues.append(f"visual_decomposition_missing_primary_medical_object:{card.get('title') or idx}")
    return issues


def crop_plan_from_component_map(component_map: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    units = component_map.get("units") if isinstance(component_map, dict) else []
    if not isinstance(units, list):
        units = []
    crop_items: list[dict[str, Any]] = []
    for idx, card in enumerate(cards, start=1):
        title = str(card.get("title") or card.get("new_element") or card.get("visual_element") or f"Unit {idx}")
        policy = str(card.get("source_policy") or "").lower()
        if policy in {"replace_with_new", "generate_new"}:
            crop_items.append({
                "unit_id": card.get("id") or card.get("unit_id") or f"unit_{idx}",
                "title": title,
                "source_policy": policy,
                "raw_crop_strategy": "impossible_use_generate_new",
                "crop_parts": [],
                "quality_risk": "low",
                "notes": "Replacement/generate_new element: do not crop old source visual.",
            })
            continue
        keys = _card_keys(card, idx)
        matched = None
        for unit in units:
            if isinstance(unit, dict) and keys & _unit_keys(unit):
                matched = unit
                break
        crop_parts = []
        if matched:
            comps = [c for c in (matched.get("components") or []) if isinstance(c, dict)]
            comps.sort(key=component_sort_key)
            for comp in comps:
                if str(comp.get("action") or "").lower() != "preserve":
                    continue
                ctype = str(comp.get("type") or "visual")
                if ctype in {"text_label", "background", "watermark", "social_ui", "decorative"}:
                    continue
                bbox = comp.get("bbox")
                if not isinstance(bbox, dict):
                    continue
                crop_parts.append({
                    "part": comp.get("component_id") or ctype,
                    "component_type": ctype,
                    "semantic_role": comp.get("semantic_role") or component_role(comp),
                    "priority": comp.get("priority") or component_role(comp),
                    "bbox": bbox,
                    "keep": comp.get("keep") or [ctype],
                    "remove": comp.get("remove") or ["text labels", "old background", "watermark", "social UI"],
                    "boundary_type": comp.get("boundary_type") or "unknown",
                })
        if not crop_parts:
            strategy = "impossible_use_generate_new"
        elif len(crop_parts) == 1:
            strategy = "single_bbox"
        else:
            strategy = "multi_part_bbox"
        crop_items.append({
            "unit_id": card.get("id") or card.get("unit_id") or f"unit_{idx}",
            "title": title,
            "source_policy": card.get("source_policy") or "use_reference_and_clean",
            "raw_crop_strategy": strategy,
            "crop_parts": crop_parts,
            "combine_mode": "compose_clean_visual" if len(crop_parts) > 1 else "clean_single_visual",
            "quality_risk": "medium" if not crop_parts else "low",
            "notes": "Generated from Visual Decomposition Engine component map.",
        })
    return {"crop_plan": crop_items, "source": "visual_decomposition_engine"}
