from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset
from app.prompts.semantic_reconstruction import (
    SEMANTIC_RECONSTRUCTION_SYSTEM_PROMPT,
    SEMANTIC_RECONSTRUCTION_USER_TEMPLATE,
)
from app.schemas.project_state import ContinuationPackage, ProjectStatePayload
from app.services.project_state_manager import create_project_state
from app.services.telegram_bot import download_file_bytes
from app.services.cost_tracker import (
    aggregate_costs,
    cost_from_response_usage,
    save_cost_event,
)
from app.services.semantic_analysis_store import save_analysis_to_db


class SemanticReconstructionError(RuntimeError):
    pass


def _client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise SemanticReconstructionError("OPENAI_API_KEY не задан.")
    return OpenAI(api_key=settings.openai_api_key)


def _cut(value: str | None, limit: int = 2500) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n...обрезано"


def _extract_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise SemanticReconstructionError("AI did not return JSON")
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise SemanticReconstructionError("AI JSON root must be an object")
    return data


def _asset_image_content(asset: ContentAsset) -> list[dict[str, Any]]:
    if not asset.media_file_id or not (asset.media_type or "").lower() in {"photo", "image", "document"}:
        return []
    try:
        image_bytes = download_file_bytes(asset.media_file_id)
    except Exception:
        return []
    if not image_bytes:
        return []
    mime = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return [{"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}]


def _normalize_size(size: Any, default: int = 512) -> Dict[str, Any]:
    """Normalize semantic PNG output size.

    v42.3 supports compact dynamic sizing. The analysis may return
    {"mode": "auto_from_layout"}; the image pipeline then computes the
    real target size from the Composer grid. Explicit numeric sizes remain
    backward-compatible.
    """
    if isinstance(size, dict) and str(size.get("mode") or "").lower() == "auto_from_layout":
        return {"mode": "auto_from_layout"}
    if not isinstance(size, dict):
        return {"mode": "auto_from_layout"}
    def _n(v: Any) -> int:
        try:
            return max(128, min(1024, int(v)))
        except Exception:
            return default
    return {"w": _n(size.get("w", default)), "h": _n(size.get("h", default))}


def normalize_semantic_plan(data: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    issues: List[str] = []

    entities = data.get("visual_entity_map")
    if not isinstance(entities, list):
        entities = []
        issues.append("visual_entity_map_missing_or_invalid")

    normalized_entities: List[Dict[str, Any]] = []
    for i, entity in enumerate(entities, start=1):
        if not isinstance(entity, dict):
            issues.append(f"invalid_entity_{i}")
            continue
        entity_id = str(entity.get("entity_id") or f"entity_{i:03d}")
        entity["entity_id"] = entity_id
        decision = str(entity.get("decision") or "keep").lower()
        if decision not in {"keep", "preserve_visual_rewrite_text", "remove", "replace", "hybrid", "merge", "generate_new"}:
            issues.append(f"invalid_decision:{entity_id}:{decision}")
            decision = "keep"
        entity["decision"] = decision
        components = entity.get("components")
        entity["components"] = components if isinstance(components, list) else []
        # v41.2 compact prompt may omit component lists and entity-level semantic_png.
        # Detailed extraction instructions live in semantic_png_plan, so do not treat this as a validation issue.
        for key in ("preserve_components", "remove_components", "generate_components"):
            value = entity.get(key)
            if not isinstance(value, list):
                entity[key] = []
        if not str(entity.get("semantic_png_description") or "").strip() and entity.get("decision") not in {"remove", "merge"}:
            entity["semantic_png_description"] = str(entity.get("final_label") or entity.get("source_label") or entity_id)
        semantic_png = entity.get("semantic_png")
        if not isinstance(semantic_png, dict):
            role = str(entity.get("entity_role") or "").lower()
            # v42.1: Composer renders text/layout blocks; never infer PNG need for ambiguous roles.
            # Only explicit visual comparison/icon roles may be considered image-bearing.
            visual_roles = {"comparison_item", "hero_visual", "visual_explanation", "icon"}
            needs_png = decision not in {"remove", "merge"} and role in visual_roles
            semantic_png = {"needed": needs_png, "operation": "extract_from_source"}
        entity["semantic_png"] = semantic_png
        normalized_entities.append(entity)

    data["visual_entity_map"] = normalized_entities

    plan = data.get("semantic_png_plan")
    if not isinstance(plan, list):
        plan = []
        issues.append("semantic_png_plan_missing_or_invalid")

    normalized_plan: List[Dict[str, Any]] = []
    seen_png_ids: set[str] = set()
    for i, task in enumerate(plan, start=1):
        if not isinstance(task, dict):
            issues.append(f"invalid_semantic_png_task_{i}")
            continue
        png_id = str(task.get("png_id") or f"png_{i:03d}")
        if png_id in seen_png_ids:
            png_id = f"{png_id}_{i}"
            issues.append(f"duplicate_png_id_fixed:{png_id}")
        seen_png_ids.add(png_id)
        task["png_id"] = png_id
        task["entity_id"] = str(task.get("entity_id") or "")
        op = str(task.get("operation") or "extract_from_source").lower()
        if op not in {"extract_from_source", "generate_new"}:
            issues.append(f"invalid_png_operation:{png_id}:{op}")
            op = "extract_from_source"
        task["operation"] = op
        task["must_include"] = task.get("must_include") if isinstance(task.get("must_include"), list) else []
        task["must_exclude"] = task.get("must_exclude") if isinstance(task.get("must_exclude"), list) else []
        task["output_size"] = _normalize_size(task.get("output_size"))
        task["transparent_background"] = bool(task.get("transparent_background", True))
        q = str(task.get("quality_strategy") or ("extract_no_upscale" if op == "extract_from_source" else "regenerate_high_detail")).lower()
        if q not in {"preserve_original_resolution", "extract_no_upscale", "regenerate_high_detail", "redraw_from_reference"}:
            issues.append(f"invalid_quality_strategy:{png_id}:{q}")
            q = "extract_no_upscale" if op == "extract_from_source" else "regenerate_high_detail"
        task["quality_strategy"] = q
        hint = task.get("source_crop_hint")
        if not isinstance(hint, dict):
            hint = {}
        box = hint.get("relative_box")
        if not (isinstance(box, list) and len(box) == 4):
            box = None
            if op == "extract_from_source":
                issues.append(f"source_crop_hint_missing:{png_id}")
        hint["relative_box"] = box
        try:
            hint["confidence"] = float(hint.get("confidence") or 0)
        except Exception:
            hint["confidence"] = 0.0
        task["source_crop_hint"] = hint
        normalized_plan.append(task)

    # v42.1: do NOT autocreate semantic PNG tasks.
    # Auto-created tasks caused header/footer/layout text blocks to become bogus PNGs.
    # Missing tasks are reported as validation issues and must be fixed by the analysis prompt,
    # not silently invented by backend code.
    data["semantic_png_plan"] = normalized_plan

    # Validate that every entity needing semantic PNG is covered by a task.
    plan_entity_ids = {str(t.get("entity_id") or "") for t in normalized_plan}
    for entity in normalized_entities:
        entity_id = entity.get("entity_id")
        if not entity_id or entity.get("decision") in {"remove", "merge"}:
            continue
        semantic_png = entity.get("semantic_png") or {}
        if semantic_png.get("needed") is False:
            continue
        role = str(entity.get("entity_role") or "").lower()
        if role in {"header", "footer", "warning", "instruction", "text_block"}:
            continue
        if entity_id not in plan_entity_ids:
            issues.append(f"semantic_png_plan_missing_for_entity:{entity_id}")

    valid_entity_ids = {e.get("entity_id") for e in normalized_entities}
    entity_by_id = {str(e.get("entity_id")): e for e in normalized_entities if e.get("entity_id")}
    for task in normalized_plan:
        entity_id = task.get("entity_id")
        if entity_id and entity_id not in valid_entity_ids:
            issues.append(f"semantic_png_plan_unknown_entity:{task.get('png_id')}:{entity_id}")
            continue
        entity = entity_by_id.get(str(entity_id or ""))
        if not entity:
            continue
        role = str(entity.get("entity_role") or "").lower()
        if role in {"header", "footer", "warning", "instruction", "text_block"}:
            issues.append(f"semantic_png_plan_for_layout_entity:{task.get('png_id')}:{entity_id}")
        recommended = str(entity.get("recommended_action") or "").lower()
        decision = str(entity.get("decision") or "").lower()
        operation = str(task.get("operation") or "").lower()
        try:
            score = int(float(entity.get("visual_quality_score") or 0))
        except Exception:
            score = 0
        if recommended == "preserve" and operation == "generate_new":
            issues.append(f"preserve_entity_has_generate_new_task:{task.get('png_id')}:{entity_id}")
            # If the entity is explicitly preserved, prefer deterministic extraction.
            if decision in {"keep", "preserve_visual_rewrite_text", "hybrid"}:
                task["operation"] = "extract_from_source"
                if task.get("quality_strategy") in {"regenerate_high_detail", "redraw_from_reference"}:
                    task["quality_strategy"] = "extract_no_upscale"
        if score >= 80 and recommended in {"", "preserve"} and operation == "generate_new" and decision not in {"replace", "generate_new"}:
            issues.append(f"high_quality_visual_should_not_generate_new:{task.get('png_id')}:{entity_id}")
            task["operation"] = "extract_from_source"
            if task.get("quality_strategy") in {"regenerate_high_detail", "redraw_from_reference"}:
                task["quality_strategy"] = "extract_no_upscale"

    # v42.4: normalize universal visual template groups for style-locked generation.
    raw_groups = data.get("visual_template_groups")
    normalized_groups: List[Dict[str, Any]] = []
    if isinstance(raw_groups, list):
        valid_ids = {str(e.get("entity_id")) for e in normalized_entities if e.get("entity_id")}
        png_by_entity = {str(t.get("entity_id")): str(t.get("png_id")) for t in normalized_plan if t.get("entity_id") and t.get("png_id")}
        for gi, group in enumerate(raw_groups, start=1):
            if not isinstance(group, dict):
                continue
            members = [str(x) for x in (group.get("members") or []) if str(x) in valid_ids]
            if len(members) < 3 and str(group.get("template_mode") or "").lower() == "shared":
                issues.append(f"visual_template_group_too_small:{group.get('group_id') or gi}")
            mode = str(group.get("template_mode") or "independent").lower()
            if mode not in {"shared", "independent"}:
                mode = "independent"
                issues.append(f"invalid_template_mode:{group.get('group_id') or gi}")
            ref_entity = str(group.get("reference_entity_id") or "")
            if ref_entity not in valid_ids:
                # Prefer a preserved/extracted entity if possible, otherwise first member.
                ref_entity = ""
                for mid in members:
                    ent = entity_by_id.get(mid) or {}
                    dec = str(ent.get("decision") or "").lower()
                    rec = str(ent.get("recommended_action") or "").lower()
                    if dec in {"keep", "preserve_visual_rewrite_text", "hybrid"} or rec in {"preserve", "hybrid"}:
                        ref_entity = mid
                        break
                if not ref_entity and members:
                    ref_entity = members[0]
            ref_png = str(group.get("reference_png_id") or "") or png_by_entity.get(ref_entity, "")
            normalized_groups.append({
                "group_id": str(group.get("group_id") or f"group_{gi}"),
                "template_mode": mode,
                "similarity": float(group.get("similarity") or 0.0),
                "members": members,
                "reference_entity_id": ref_entity or None,
                "reference_png_id": ref_png or None,
                "invariant_features": _compact_list(group.get("invariant_features"), 4, 60),
                "variable_features": _compact_list(group.get("variable_features"), 4, 60),
            })
    data["visual_template_groups"] = normalized_groups

    # Attach style_lock_group/reference_png_id to generate_new tasks in shared groups.
    entity_to_group: Dict[str, Dict[str, Any]] = {}
    for group in normalized_groups:
        if group.get("template_mode") != "shared":
            continue
        for mid in group.get("members") or []:
            entity_to_group[str(mid)] = group
    for task in normalized_plan:
        if str(task.get("operation") or "").lower() != "generate_new":
            continue
        group = entity_to_group.get(str(task.get("entity_id") or ""))
        if not group:
            continue
        task.setdefault("style_lock_group", group.get("group_id"))
        if not task.get("reference_png_id"):
            task["reference_png_id"] = group.get("reference_png_id")

    loc = data.get("label_object_consistency")
    if isinstance(loc, dict) and loc.get("is_consistent") is False:
        issues.append(f"label_object_inconsistency:{loc.get('issue') or 'unclear'}")

    # v43.1 hotfix: normalize smart blocks at analysis boundary, keep max 3,
    # and avoid duplicating the legal footer warning block.
    raw_smart_blocks = data.get("smart_blocks")
    if isinstance(raw_smart_blocks, list) and len(raw_smart_blocks) > 3:
        issues.append("smart_blocks_trimmed_to_3")
    normalized_smart_blocks = _normalize_smart_blocks(raw_smart_blocks)
    bp_for_blocks = data.get("design_blueprint") if isinstance(data.get("design_blueprint"), dict) else {}
    raw_footer_blocks = bp_for_blocks.get("footer_blocks") if isinstance(bp_for_blocks.get("footer_blocks"), list) else []
    footer_titles = {str(b.get("title") or "").strip().lower() for b in raw_footer_blocks if isinstance(b, dict)}
    if footer_titles:
        filtered_smart_blocks: List[Dict[str, Any]] = []
        for block in normalized_smart_blocks:
            title_l = str(block.get("title") or "").strip().lower()
            if block.get("block_type") == "important_note" and (title_l in footer_titles or "важно" in footer_titles):
                issues.append("smart_block_duplicated_footer_removed:important_note")
                continue
            filtered_smart_blocks.append(block)
        normalized_smart_blocks = filtered_smart_blocks
    if normalized_smart_blocks and all(str(b.get("priority")) == "low" for b in normalized_smart_blocks):
        issues.append("smart_blocks_all_low_priority")
    audit_for_blocks = data.get("medical_editorial_audit") if isinstance(data.get("medical_editorial_audit"), dict) else {}
    has_medical_warnings = bool(audit_for_blocks.get("risks") or audit_for_blocks.get("footer_warnings") or audit_for_blocks.get("required_warnings"))
    safety_types = {"when_doctor", "important_note", "contraindications", "first_aid"}
    if has_medical_warnings and not any(b.get("block_type") in safety_types for b in normalized_smart_blocks):
        issues.append("smart_blocks_no_safety_block")
    data["smart_blocks"] = normalized_smart_blocks

    if not isinstance(data.get("design_blueprint"), dict):
        data["design_blueprint"] = {}
        issues.append("design_blueprint_missing_or_invalid")
    if not isinstance(data.get("post"), dict):
        data["post"] = {}
        issues.append("post_missing_or_invalid")
    if not isinstance(data.get("qa_checklist"), list):
        data["qa_checklist"] = []
        issues.append("qa_checklist_missing_or_invalid")

    return data, issues




def _compact_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_list(value: Any, max_items: int = 4, item_limit: int = 120) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        if len(out) >= max_items:
            break
        s = _compact_text(item, item_limit)
        if s:
            out.append(s)
    return out


def _normalize_review_issue(issue: Any) -> str:
    raw = _compact_text(issue, 40).lower()
    aliases = {
        "medical_risk": "unsafe_claim",
        "medical_claim": "unsafe_claim",
        "dangerous_claim": "unsafe_claim",
        "wrong_claim": "factual_error",
        "false_claim": "factual_error",
    }
    return aliases.get(raw, raw)


def _compact_examples(value: Any) -> List[str]:
    """Examples are source facts (drug/product/method names), kept separate from short_text."""
    return _compact_list(value, 6, 34)


def _compact_review_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Ultra-compact review: enough for audit/debug, not a second explanation layer."""
    entity_id = item.get("entity_id") or item.get("source_entity_id")
    decision = item.get("decision") or item.get("selected_decision")
    replacement = item.get("replacement") if "replacement" in item else item.get("selected_replacement")
    issue = _normalize_review_issue(item.get("issue") or item.get("initial_problem"))
    compact = {
        "entity_id": entity_id,
        "issue": issue,
        "decision": _compact_text(decision, 24),
        "replacement": replacement,
    }
    return {k: v for k, v in compact.items() if v not in (None, "", [], {})}



def _important_review_item(item: Dict[str, Any]) -> bool:
    """Keep replacement_review compact: retain changed/problematic decisions, omit routine keep/none."""
    issue = _normalize_review_issue(item.get("issue") or item.get("initial_problem") or "")
    decision = str(item.get("decision") or item.get("selected_decision") or "").lower()
    replacement = item.get("replacement") if "replacement" in item else item.get("selected_replacement")
    if decision in {"replace", "hybrid", "merge", "remove", "preserve_visual_rewrite_text"}:
        return True
    if issue and issue != "none":
        return True
    if replacement:
        return True
    return False


def _compact_audit(audit: Any) -> Dict[str, Any]:
    """Deprecated in v43.5 compact: safety content is stored in smart_blocks/footer."""
    return {}



def _compact_source_pattern(pattern: Any) -> Dict[str, Any]:
    pattern = pattern if isinstance(pattern, dict) else {}
    return {
        "structure": _compact_text(pattern.get("structure"), 150),
        "visual_strengths": _compact_list(pattern.get("visual_strengths"), 2, 60),
        "what_to_preserve": _compact_list(pattern.get("what_to_preserve"), 3, 60),
        "what_to_fix": _compact_list(pattern.get("what_to_fix"), 3, 60),
    }


def _compact_png_task(task: Dict[str, Any]) -> Dict[str, Any]:
    hint = task.get("source_crop_hint") if isinstance(task.get("source_crop_hint"), dict) else {}
    crop_hint = {
        "relative_box": hint.get("relative_box"),
        "confidence": hint.get("confidence"),
        "note": _compact_text(hint.get("note"), 60),
    }
    out = {
        "png_id": task.get("png_id"),
        "entity_id": task.get("entity_id"),
        "operation": task.get("operation"),
        "quality_strategy": task.get("quality_strategy"),
        "source_crop_hint": {k: v for k, v in crop_hint.items() if v not in (None, "", [], {})},
        "instruction_for_python_or_image_ai": _compact_text(task.get("instruction_for_python_or_image_ai"), 190),
        "must_include": _compact_list(task.get("must_include"), 4, 60),
        "must_exclude": _compact_list(task.get("must_exclude"), 4, 60),
        "reference_png_id": task.get("reference_png_id"),
        "style_lock_group": task.get("style_lock_group"),
        "output_size": task.get("output_size"),
        "transparent_background": bool(task.get("transparent_background", True)),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}

def _is_layout_entity(entity: Dict[str, Any]) -> bool:
    role = str(entity.get("entity_role") or "").lower()
    source = (str(entity.get("source_label") or "") + " " + str(entity.get("final_label") or "")).lower()
    return role in {"header", "footer", "layout", "ui", "ui_element"} or "интерфейс" in source or "кноп" in source




SMART_BLOCK_DEFAULTS: Dict[str, Dict[str, str]] = {
    "when_doctor": {"title": "Когда к врачу", "icon_role": "doctor", "color_role": "danger"},
    "first_aid": {"title": "Что сделать сразу", "icon_role": "first_aid", "color_role": "care"},
    "prevention": {"title": "Как защититься", "icon_role": "shield", "color_role": "safe"},
    "important_note": {"title": "Важно", "icon_role": "alert", "color_role": "warning"},
    "contraindications": {"title": "Когда нельзя без врача", "icon_role": "alert", "color_role": "danger"},
    "how_to_choose": {"title": "Как выбрать", "icon_role": "check", "color_role": "info"},
    "checklist": {"title": "Проверьте себя", "icon_role": "check", "color_role": "info"},
    "normal_variability": {"title": "Что может быть нормой", "icon_role": "info", "color_role": "info"},
    "screening_note": {"title": "Что проверить", "icon_role": "doctor", "color_role": "info"},
}


def _normalize_smart_blocks(value: Any) -> List[Dict[str, Any]]:
    """v43.1 Smart Additional Blocks hotfix.

    AI decides only: block type, priority and short items. Python supplies safe
    default titles/icons/colors and caps the output to 3 blocks for Composer v43.2.
    """
    if not isinstance(value, list):
        return []
    allowed_types = set(SMART_BLOCK_DEFAULTS)
    allowed_priority = {"high", "medium", "low"}
    allowed_icons = {"alert", "shield", "first_aid", "check", "info", "doctor", "pill", "thermometer", "phone", "leaf", "heart", "none"}
    allowed_colors = {"danger", "safe", "care", "info", "warning", "neutral"}
    out: List[Dict[str, Any]] = []
    seen_types: set[str] = set()
    for i, block in enumerate(value, start=1):
        if len(out) >= 3:
            break
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("block_type") or "important_note").lower()
        if block_type not in allowed_types:
            block_type = "important_note"
        if block_type in seen_types:
            continue
        defaults = SMART_BLOCK_DEFAULTS.get(block_type, SMART_BLOCK_DEFAULTS["important_note"])
        priority = str(block.get("priority") or "medium").lower()
        if priority not in allowed_priority:
            priority = "medium"
        icon_role = str(block.get("icon_role") or defaults["icon_role"]).lower()
        if icon_role not in allowed_icons:
            icon_role = defaults["icon_role"]
        color_role = str(block.get("color_role") or defaults["color_role"]).lower()
        if color_role not in allowed_colors:
            color_role = defaults["color_role"]
        title = _compact_text(block.get("title"), 42) or defaults["title"]
        items = _compact_list(block.get("items"), 4, 70)
        if not items:
            continue
        seen_types.add(block_type)
        out.append({
            "block_id": str(block.get("block_id") or f"smart_{len(out) + 1:03d}"),
            "block_type": block_type,
            "priority": priority,
            "title": title,
            "items": items,
            "icon_role": icon_role,
            "color_role": color_role,
        })
    # Sort high -> medium -> low, stable inside same priority.
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda b: order.get(str(b.get("priority")), 1))
    return out

def compact_semantic_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    v43.5-compact-analysis: minimal storage contract.
    Убирает дубли: entity = решение, semantic_png_plan = детали PNG,
    content_pack = тексты, design_blueprint = layout.
    """
    data = dict(data)
    entities = data.get("visual_entity_map") if isinstance(data.get("visual_entity_map"), list) else []
    visual_entities: List[Dict[str, Any]] = []
    layout_entities: List[Dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        target = layout_entities if _is_layout_entity(entity) else visual_entities
        compact = {
            "entity_id": entity.get("entity_id"),
            "source_label": _compact_text(entity.get("source_label"), 55),
            "final_label": _compact_text(entity.get("final_label"), 55),
            "entity_role": entity.get("entity_role"),
            "decision": entity.get("decision"),
            "visual_quality_score": entity.get("visual_quality_score"),
            "recommended_action": entity.get("recommended_action"),
            "reason": _compact_text(entity.get("reason") or entity.get("preservation_reason"), 110),
            "reference_entity_id": entity.get("reference_entity_id"),
        }
        target.append({k: v for k, v in compact.items() if v not in (None, "", [], {})})

    bp = data.get("design_blueprint") if isinstance(data.get("design_blueprint"), dict) else {}
    raw_cards = bp.get("cards") if isinstance(bp.get("cards"), list) else []
    content_cards: List[Dict[str, Any]] = []
    layout_cards: List[Dict[str, Any]] = []
    for card in raw_cards:
        if not isinstance(card, dict):
            continue
        content_cards.append({
            "card_id": card.get("card_id"),
            "entity_id": card.get("entity_id"),
            "png_id": card.get("png_id"),
            "title": _compact_text(card.get("title"), 55),
            "short_text": _compact_text(card.get("short_text"), 130),
            "examples": _compact_examples(card.get("examples")),
        })
        layout_cards.append({
            "card_id": card.get("card_id"),
            "entity_id": card.get("entity_id"),
            "png_id": card.get("png_id"),
            "design_instruction": _compact_text(card.get("design_instruction"), 95),
        })

    header = bp.get("header") if isinstance(bp.get("header"), dict) else {}
    compact_header = {
        "text": _compact_text(header.get("text"), 100),
        "subtitle": _compact_text(header.get("subtitle"), 120),
        "design_instruction": _compact_text(header.get("design_instruction"), 110),
    }
    footer_blocks = []
    for block in bp.get("footer_blocks") or []:
        if isinstance(block, dict):
            footer_blocks.append({
                "block_id": block.get("block_id"),
                "title": _compact_text(block.get("title"), 55),
                "text": _compact_text(block.get("text"), 160),
                "design_instruction": _compact_text(block.get("design_instruction"), 90),
            })

    style = bp.get("style") if isinstance(bp.get("style"), dict) else {}
    compact_bp = {
        "canvas": bp.get("canvas", {}),
        "style": {
            "direction": _compact_text(style.get("direction"), 100),
            "colors": _compact_list(style.get("colors"), 6, 20),
            "typography": _compact_text(style.get("typography"), 90),
            "mood": _compact_text(style.get("mood"), 75),
        },
        "layout": _compact_text(bp.get("layout"), 180),
        "header": compact_header,
        "cards": [{k: v for k, v in card.items() if v not in (None, "", [], {})} for card in layout_cards],
        "footer_blocks": [{k: v for k, v in block.items() if v not in (None, "", [], {})} for block in footer_blocks],
    }

    post = data.get("post") if isinstance(data.get("post"), dict) else {}
    compact_post = {
        "title": _compact_text(post.get("title"), 90),
        "body": _compact_text(post.get("body"), 650),
        "cta": _compact_text(post.get("cta"), 130),
    }
    content_pack = {
        "header": {k: v for k, v in compact_header.items() if v not in (None, "", [], {})},
        "cards": [{k: v for k, v in card.items() if v not in (None, "", [], {})} for card in content_cards],
        "footer_blocks": [{k: v for k, v in block.items() if v not in (None, "", [], {})} for block in footer_blocks],
    }

    return {
        "asset_type": data.get("asset_type"),
        "topic": _compact_text(data.get("topic"), 130),
        "source_pattern": _compact_source_pattern(data.get("source_pattern")),
        "schema_version": data.get("schema_version") or "v43.9-compact-reconstruction",
        "audience": data.get("audience") or "general_public",
        "source_item_count_estimate": data.get("source_item_count_estimate"),
        "source_label_count_estimate": data.get("source_label_count_estimate"),
        "final_card_count": data.get("final_card_count"),
        "label_object_consistency": data.get("label_object_consistency") if isinstance(data.get("label_object_consistency"), dict) else {},
        "visual_template_groups": data.get("visual_template_groups") if isinstance(data.get("visual_template_groups"), list) else [],
        "smart_blocks": _normalize_smart_blocks(data.get("smart_blocks")),
        "replacement_review": [_compact_review_item(x) for x in (data.get("replacement_review") or []) if isinstance(x, dict) and _important_review_item(x)],
        "visual_entity_map": visual_entities,
        "layout_entities": layout_entities,
        "semantic_png_plan": [_compact_png_task(x) for x in (data.get("semantic_png_plan") or []) if isinstance(x, dict)],
        "design_blueprint": compact_bp,
        "content_pack": content_pack,
        "post": {k: v for k, v in compact_post.items() if v not in (None, "", [], {})},
        "qa_checklist": _compact_list(data.get("qa_checklist"), 6, 90),
    }


def save_semantic_analysis_json(asset_id: int, state_id: int, payload: ProjectStatePayload, issues: List[str]) -> str:
    analysis_dir = Path("storage/analysis")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"asset-{asset_id}-state-{state_id}-semantic-analysis.json"
    data = {
        "asset_id": asset_id,
        "project_state_id": state_id,
        "pipeline_stage": "semantic_analysis",
        "schema_version": "v43.9-compact-reconstruction",
        "validation_issues": issues,
        "payload": payload.model_dump(),
    }
    output_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return str(output_path)


def run_semantic_reconstruction_analysis(db: Session, asset_id: int):
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if asset is None:
        raise SemanticReconstructionError(f"Asset #{asset_id} not found")

    # ВАЖНО: не используем .format() для этого шаблона.
    # Внутри SEMANTIC_RECONSTRUCTION_USER_TEMPLATE есть большой пример JSON с фигурными скобками.
    # .format() воспринимает ключи JSON как переменные и падает с ошибкой вида: KeyError: '\n  "asset_type"'.
    user_prompt = (
        SEMANTIC_RECONSTRUCTION_USER_TEMPLATE
        .replace("{asset_id}", str(asset.id))
        .replace("{source_type}", asset.source_type or "")
        .replace("{media_type}", asset.media_type or "")
        .replace("{caption}", _cut(asset.caption))
        .replace("{text_content}", _cut(asset.text_content))
        .replace("{source_url}", asset.source_url or "")
        .replace("{asset_classification}", _cut(asset.analysis, 800))
    )

    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
    content.extend(_asset_image_content(asset))

    response = _client().responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SEMANTIC_RECONSTRUCTION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    cost_summary = aggregate_costs([
        cost_from_response_usage(
            operation="semantic_analysis",
            model=settings.openai_model,
            response=response,
            metadata={"asset_id": asset_id},
        )
    ])
    save_cost_event("semantic_analysis", asset_id, cost_summary)

    raw_data = _extract_json(response.output_text)
    normalized, issues = normalize_semantic_plan(raw_data)

    continuation = ContinuationPackage(
        current_state_summary=f"Semantic analysis #{asset_id}: {normalized.get('topic', '')}",
        last_successful_stage="semantic_analysis",
    )

    compact = compact_semantic_payload(normalized)

    payload = ProjectStatePayload(
        analysis_state={
            "asset_type": compact.get("asset_type"),
            "topic": compact.get("topic"),
            "source_pattern": compact.get("source_pattern", {}),
            "validation_issues": issues,
        },
        visual_entity_map=compact.get("visual_entity_map", []),
        semantic_png_plan=compact.get("semantic_png_plan", []),
        design_blueprint=compact.get("design_blueprint", {}),
        post=compact.get("post", {}),
        qa_checklist=compact.get("qa_checklist", []),
        continuation_package=continuation,
        custom={
            "schema_version": "v43.9-compact-reconstruction",
            "layout_entities": compact.get("layout_entities", []),
            "content_pack": compact.get("content_pack", {}),
            "replacement_review": compact.get("replacement_review", []),
            "source_item_count_estimate": compact.get("source_item_count_estimate"),
            "source_label_count_estimate": compact.get("source_label_count_estimate"),
            "final_card_count": compact.get("final_card_count"),
            "label_object_consistency": compact.get("label_object_consistency", {}),
            "visual_template_groups": compact.get("visual_template_groups", []),
            "smart_blocks": compact.get("smart_blocks", []),
            "audience": compact.get("audience", "general_public"),
            "validation_issues": issues,
            "cost_estimate": cost_summary,
        },
    )

    state = create_project_state(
        db,
        asset_id=asset_id,
        pipeline_stage="semantic_analysis",
        payload=payload,
    )

    analysis_path = save_semantic_analysis_json(asset_id, state.id, payload, issues)
    payload.custom["analysis_json_path"] = analysis_path
    payload.analysis_state["analysis_json_path"] = analysis_path
    payload.analysis_state["cost_estimate"] = cost_summary
    from app.services.project_state_manager import update_project_state

    state = update_project_state(
        db,
        state.id,
        pipeline_stage="semantic_analysis",
        payload=payload,
        stage_result={"analysis_json_path": analysis_path, "validation_issues": issues},
    )

    # Canonical persistent copy of the expensive analysis stage.
    # Local storage/analysis files are convenience exports only and may disappear on Railway redeploy.
    save_analysis_to_db(
        db,
        asset_id=asset_id,
        state_id=state.id,
        payload=payload,
        issues=issues,
        file_path=analysis_path,
    )
    return state, issues
