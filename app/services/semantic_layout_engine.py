"""Semantic Layout Reconstruction Pipeline helpers.

This module is intentionally placed before component decomposition/crop. It
creates a layout-first map of the source infographic, then helps later stages
avoid two common failures:
1) splitting one semantic card into several object cards;
2) cropping salient objects instead of components inside the correct card.
"""
from __future__ import annotations

import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image

from app.prompts.semantic_layout import SEMANTIC_LAYOUT_SYSTEM_PROMPT, SEMANTIC_LAYOUT_USER_TEMPLATE


def _image_data_url(image: Image.Image) -> str:
    buf = BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def _safe_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower().replace("ё", "е"))


def build_semantic_layout_map(
    *,
    client: OpenAI,
    model: str,
    source_image: Image.Image,
    cards: list[dict[str, Any]],
    contract_summary_text: str,
) -> dict[str, Any]:
    cards_json = []
    for i, card in enumerate(cards, start=1):
        cards_json.append({
            "idx": i,
            "id": card.get("id") or card.get("unit_id") or f"unit_{i}",
            "title": card.get("title") or card.get("new_element") or card.get("visual_element") or f"Unit {i}",
            "source_policy": card.get("source_policy"),
            "old_element": card.get("old_element"),
            "new_element": card.get("new_element"),
            "lines": card.get("lines"),
        })
    prompt = SEMANTIC_LAYOUT_USER_TEMPLATE.format(
        cards_json=json.dumps(cards_json, ensure_ascii=False, indent=2),
        contract_summary=contract_summary_text,
    )
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": SEMANTIC_LAYOUT_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": _image_data_url(source_image)},
            ]},
        ],
    )
    return normalize_semantic_layout(_safe_json(response.output_text))


def normalize_semantic_layout(layout: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(layout, dict):
        return {}
    out = dict(layout)
    cards = out.get("semantic_cards")
    if not isinstance(cards, list):
        out["semantic_cards"] = []
        return out
    norm_cards = []
    for idx, c in enumerate(cards, start=1):
        if not isinstance(c, dict):
            continue
        nc = dict(c)
        nc.setdefault("card_id", f"source_card_{idx}")
        nc.setdefault("card_role", "comparison_item")
        comps = nc.get("components")
        if not isinstance(comps, list):
            nc["components"] = []
        norm_cards.append(nc)
    out["semantic_cards"] = norm_cards
    return out


def validate_semantic_layout(layout: dict[str, Any], cards: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    semantic_cards = layout.get("semantic_cards") if isinstance(layout, dict) else None
    if not isinstance(semantic_cards, list) or not semantic_cards:
        return ["semantic_layout_no_cards"]
    # Layout count should not explode far beyond planned cards. This catches cases
    # like splitting "Wasp / Yellow Jacket" into two independent cards.
    planned = max(1, len(cards))
    if len(semantic_cards) > planned + max(1, planned // 4):
        issues.append(f"semantic_layout_too_many_cards:{len(semantic_cards)}_planned_{planned}")
    for idx, c in enumerate(semantic_cards, start=1):
        title = c.get("source_title") or c.get("translated_title") or c.get("card_id") or idx
        bbox = c.get("card_bbox")
        if not isinstance(bbox, dict):
            issues.append(f"semantic_layout_missing_card_bbox:{title}")
        else:
            try:
                x = float(bbox.get("x", -1)); y = float(bbox.get("y", -1)); w = float(bbox.get("w", 0)); h = float(bbox.get("h", 0))
                if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1):
                    issues.append(f"semantic_layout_bad_card_bbox:{title}")
            except Exception:
                issues.append(f"semantic_layout_bad_card_bbox:{title}")
        if c.get("is_one_logical_card") is False and "/" in str(c.get("source_title") or ""):
            issues.append(f"semantic_layout_possible_bad_split:{title}")
        comps = c.get("components") if isinstance(c.get("components"), list) else []
        if str(c.get("card_role") or "").lower() in {"comparison_item", "card", "visual_card"}:
            has_primary = any(str(comp.get("semantic_role") or comp.get("type") or "").lower() in {"primary_medical_object", "primary_medical_visual"} for comp in comps if isinstance(comp, dict))
            if not has_primary:
                issues.append(f"semantic_layout_missing_primary_component:{title}")
    return issues


def layout_context_for_prompt(layout: dict[str, Any]) -> str:
    if not isinstance(layout, dict) or not layout.get("semantic_cards"):
        return ""
    return "\nSEMANTIC_LAYOUT_MAP:\n" + json.dumps(layout, ensure_ascii=False, indent=2)[:12000]


def semantic_card_for_title(layout: dict[str, Any], title: str) -> dict[str, Any] | None:
    nt = _norm(title)
    for card in layout.get("semantic_cards") or []:
        if not isinstance(card, dict):
            continue
        keys = {_norm(card.get("source_title")), _norm(card.get("translated_title")), _norm(card.get("recommended_final_title")), _norm(card.get("card_id"))}
        if nt and nt in keys:
            return card
    return None


def enforce_card_boundaries_on_component_map(component_map: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    """Attach semantic-card info to a component map when titles match.

    Later crop code can use this metadata to reject components that are not from
    the same semantic card.
    """
    if not isinstance(component_map, dict) or not isinstance(layout, dict):
        return component_map
    out = dict(component_map)
    units = out.get("units") if isinstance(out.get("units"), list) else []
    new_units = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        u = dict(unit)
        sc = semantic_card_for_title(layout, u.get("title") or u.get("source_title") or u.get("unit_id"))
        if sc:
            u["semantic_card_id"] = sc.get("card_id")
            u["semantic_card_bbox"] = sc.get("card_bbox")
        new_units.append(u)
    out["units"] = new_units
    return out
