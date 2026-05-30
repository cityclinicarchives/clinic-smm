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


def _normalize_size(size: Any, default: int = 512) -> Dict[str, int]:
    if not isinstance(size, dict):
        return {"w": default, "h": default}
    def _n(v: Any) -> int:
        try:
            return max(64, min(2048, int(v)))
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
        if decision not in {"keep", "remove", "replace", "merge", "generate_new"}:
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
        entity["semantic_png"] = semantic_png if isinstance(semantic_png, dict) else {"needed": decision != "remove", "operation": "extract_from_source"}
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

    # Autocomplete missing semantic PNG tasks from visual entities.
    entity_ids_with_task = {t.get("entity_id") for t in normalized_plan if t.get("entity_id")}
    for entity in normalized_entities:
        entity_id = entity["entity_id"]
        if entity.get("decision") in {"remove", "merge"}:
            continue
        semantic_png = entity.get("semantic_png") or {}
        if semantic_png.get("needed") is False:
            continue
        if entity_id in entity_ids_with_task:
            continue
        png_id = str(semantic_png.get("output_name") or f"png_{entity_id}").replace(" ", "_")
        if png_id in seen_png_ids:
            png_id = f"{png_id}_auto"
        seen_png_ids.add(png_id)
        normalized_plan.append({
            "png_id": png_id,
            "entity_id": entity_id,
            "operation": semantic_png.get("operation") if semantic_png.get("operation") in {"extract_from_source", "generate_new"} else ("generate_new" if entity.get("decision") == "replace" else "extract_from_source"),
            "quality_strategy": semantic_png.get("quality_strategy") or ("regenerate_high_detail" if entity.get("decision") == "replace" else "extract_no_upscale"),
            "source_crop_hint": semantic_png.get("source_crop_hint") if isinstance(semantic_png.get("source_crop_hint"), dict) else {},
            "instruction_for_python_or_image_ai": f"Create semantic PNG for entity {entity.get('final_label') or entity.get('source_label') or entity_id}. Preserve useful visual parts, remove text/watermark/UI/background unless required by design.",
            "must_include": semantic_png.get("must_include") if isinstance(semantic_png.get("must_include"), list) else [],
            "must_exclude": semantic_png.get("must_exclude") if isinstance(semantic_png.get("must_exclude"), list) else ["watermark", "username", "social media UI", "old labels"],
            "reference_png_id": None,
            "output_size": _normalize_size(semantic_png.get("recommended_output_size")),
            "transparent_background": True,
        })
        issues.append(f"semantic_png_task_autocreated:{entity_id}")

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
        if entity_id not in plan_entity_ids:
            issues.append(f"semantic_png_plan_missing_for_entity:{entity_id}")

    valid_entity_ids = {e.get("entity_id") for e in normalized_entities}
    for task in normalized_plan:
        entity_id = task.get("entity_id")
        if entity_id and entity_id not in valid_entity_ids:
            issues.append(f"semantic_png_plan_unknown_entity:{task.get('png_id')}:{entity_id}")

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


def _compact_review_item(item: Dict[str, Any]) -> Dict[str, Any]:
    # Supports both old verbose replacement_review and new compact schema.
    entity_id = item.get("entity_id") or item.get("source_entity_id")
    source = item.get("source") or item.get("source_label")
    decision = item.get("decision") or item.get("selected_decision")
    replacement = item.get("replacement") if "replacement" in item else item.get("selected_replacement")
    issue = item.get("issue") or item.get("initial_problem")
    reason = item.get("reason") or item.get("why_not_removed") or item.get("why_removed_if_removed")
    if not reason:
        bits = []
        for key in ("regional_analogs", "thematic_analogs", "medical_analogs"):
            vals = _compact_list(item.get(key), 1, 80)
            if vals:
                bits.append(vals[0])
        reason = "; ".join(bits)
    compact = {
        "entity_id": entity_id,
        "source": _compact_text(source, 80),
        "issue": _compact_text(issue, 40),
        "decision": _compact_text(decision, 24),
        "replacement": replacement,
        "reason": _compact_text(reason, 180),
    }
    return {k: v for k, v in compact.items() if v not in (None, "", [], {})}


def _compact_audit(audit: Any) -> Dict[str, Any]:
    audit = audit if isinstance(audit, dict) else {}
    return {
        "risks": _compact_list(audit.get("risks"), 4, 150),
        "corrections": _compact_list(audit.get("corrections"), 4, 150),
        "required_warnings": _compact_list(audit.get("required_warnings"), 4, 180),
    }


def _compact_source_pattern(pattern: Any) -> Dict[str, Any]:
    pattern = pattern if isinstance(pattern, dict) else {}
    return {
        "structure": _compact_text(pattern.get("structure"), 220),
        "what_to_preserve": _compact_list(pattern.get("what_to_preserve"), 5, 90),
    }


def _compact_png_task(task: Dict[str, Any]) -> Dict[str, Any]:
    hint = task.get("source_crop_hint") if isinstance(task.get("source_crop_hint"), dict) else {}
    crop_hint = {
        "relative_box": hint.get("relative_box"),
        "confidence": hint.get("confidence"),
        "note": _compact_text(hint.get("note"), 80),
    }
    out = {
        "png_id": task.get("png_id"),
        "entity_id": task.get("entity_id"),
        "operation": task.get("operation"),
        "quality_strategy": task.get("quality_strategy"),
        "source_crop_hint": {k: v for k, v in crop_hint.items() if v not in (None, "", [], {})},
        "instruction_for_python_or_image_ai": _compact_text(task.get("instruction_for_python_or_image_ai"), 260),
        "must_include": _compact_list(task.get("must_include"), 6, 70),
        "must_exclude": _compact_list(task.get("must_exclude"), 6, 70),
        "reference_png_id": task.get("reference_png_id"),
        "output_size": task.get("output_size"),
        "transparent_background": bool(task.get("transparent_background", True)),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}

def _is_layout_entity(entity: Dict[str, Any]) -> bool:
    role = str(entity.get("entity_role") or "").lower()
    source = (str(entity.get("source_label") or "") + " " + str(entity.get("final_label") or "")).lower()
    return role in {"header", "footer", "layout", "ui", "ui_element"} or "интерфейс" in source or "кноп" in source


def compact_semantic_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    v41.2-cost: compact storage contract.
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
            "source_label": _compact_text(entity.get("source_label"), 80),
            "final_label": _compact_text(entity.get("final_label"), 80),
            "entity_role": entity.get("entity_role"),
            "decision": entity.get("decision"),
            "reason": _compact_text(entity.get("reason"), 160),
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
            "title": _compact_text(card.get("title"), 70),
            "short_text": _compact_text(card.get("short_text"), 120),
        })
        layout_cards.append({
            "card_id": card.get("card_id"),
            "entity_id": card.get("entity_id"),
            "png_id": card.get("png_id"),
            "visual_role": _compact_text(card.get("visual_role"), 100),
            "design_instruction": _compact_text(card.get("design_instruction"), 140),
        })

    header = bp.get("header") if isinstance(bp.get("header"), dict) else {}
    compact_header = {
        "text": _compact_text(header.get("text"), 120),
        "subtitle": _compact_text(header.get("subtitle"), 140),
        "design_instruction": _compact_text(header.get("design_instruction"), 160),
    }
    footer_blocks = []
    for block in bp.get("footer_blocks") or []:
        if isinstance(block, dict):
            footer_blocks.append({
                "block_id": block.get("block_id"),
                "title": _compact_text(block.get("title"), 70),
                "text": _compact_text(block.get("text"), 180),
                "design_instruction": _compact_text(block.get("design_instruction"), 120),
            })

    style = bp.get("style") if isinstance(bp.get("style"), dict) else {}
    compact_bp = {
        "canvas": bp.get("canvas", {}),
        "style": {
            "direction": _compact_text(style.get("direction"), 160),
            "colors": _compact_list(style.get("colors"), 7, 20),
            "typography": _compact_text(style.get("typography"), 140),
            "mood": _compact_text(style.get("mood"), 100),
        },
        "layout": _compact_text(bp.get("layout"), 240),
        "header": compact_header,
        "cards": [{k: v for k, v in card.items() if v not in (None, "", [], {})} for card in layout_cards],
        "footer_blocks": [{k: v for k, v in block.items() if v not in (None, "", [], {})} for block in footer_blocks],
    }

    post = data.get("post") if isinstance(data.get("post"), dict) else {}
    compact_post = {
        "title": _compact_text(post.get("title"), 120),
        "body": _compact_text(post.get("body"), 900),
        "cta": _compact_text(post.get("cta"), 180),
    }
    content_pack = {
        "header": {k: v for k, v in compact_header.items() if v not in (None, "", [], {})},
        "cards": [{k: v for k, v in card.items() if v not in (None, "", [], {})} for card in content_cards],
        "footer_blocks": [{k: v for k, v in block.items() if v not in (None, "", [], {})} for block in footer_blocks],
    }

    return {
        "asset_type": data.get("asset_type"),
        "topic": _compact_text(data.get("topic"), 180),
        "source_pattern": _compact_source_pattern(data.get("source_pattern")),
        "medical_editorial_audit": _compact_audit(data.get("medical_editorial_audit")),
        "source_item_count_estimate": data.get("source_item_count_estimate"),
        "final_card_count": data.get("final_card_count"),
        "replacement_review": [_compact_review_item(x) for x in (data.get("replacement_review") or []) if isinstance(x, dict)],
        "image_composition_prompt": _compact_text(data.get("image_composition_prompt"), 750),
        "visual_entity_map": visual_entities,
        "layout_entities": layout_entities,
        "semantic_png_plan": [_compact_png_task(x) for x in (data.get("semantic_png_plan") or []) if isinstance(x, dict)],
        "design_blueprint": compact_bp,
        "content_pack": content_pack,
        "post": {k: v for k, v in compact_post.items() if v not in (None, "", [], {})},
        "qa_checklist": _compact_list(data.get("qa_checklist"), 12, 120),
    }


def save_semantic_analysis_json(asset_id: int, state_id: int, payload: ProjectStatePayload, issues: List[str]) -> str:
    analysis_dir = Path("storage/analysis")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"asset-{asset_id}-state-{state_id}-semantic-analysis.json"
    data = {
        "asset_id": asset_id,
        "project_state_id": state_id,
        "pipeline_stage": "semantic_analysis",
        "schema_version": "v41.2-cost-compact",
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
        current_state_summary=f"Semantic reconstruction analysis for asset #{asset_id}: {normalized.get('topic', '')}",
        strict_contract={
            "single_source_of_truth": "ProjectStatePayload",
            "do_not_hardcode_examples": True,
            "region": "Россия / Москва / Средняя полоса России",
            "visual_entity_map_required": True,
            "semantic_png_plan_required": True,
            "design_blueprint_required": True,
        },
        must_not_forget=[
            "Semantic PNG decisions are already made in visual_entity_map and semantic_png_plan.",
            "Image composition must not re-decide what to keep/remove/replace.",
            "Do not preserve watermark, username or social media UI.",
            "Do not promise exact diagnosis by image.",
        ],
        next_step_prompt="Use semantic_png_plan to extract/generate semantic PNG assets. Then compose final infographic from saved PNG assets and design_blueprint.",
        last_successful_stage="semantic_analysis",
    )

    compact = compact_semantic_payload(normalized)

    payload = ProjectStatePayload(
        analysis_state={
            "asset_type": compact.get("asset_type"),
            "topic": compact.get("topic"),
            "source_pattern": compact.get("source_pattern", {}),
            "medical_editorial_audit": compact.get("medical_editorial_audit", {}),
            "image_composition_prompt": compact.get("image_composition_prompt", ""),
            "validation_issues": issues,
        },
        visual_entity_map=compact.get("visual_entity_map", []),
        semantic_png_plan=compact.get("semantic_png_plan", []),
        design_blueprint=compact.get("design_blueprint", {}),
        post=compact.get("post", {}),
        qa_checklist=compact.get("qa_checklist", []),
        continuation_package=continuation,
        custom={
            "schema_version": "v41.2-cost-compact",
            "layout_entities": compact.get("layout_entities", []),
            "content_pack": compact.get("content_pack", {}),
            "replacement_review": compact.get("replacement_review", []),
            "source_item_count_estimate": compact.get("source_item_count_estimate"),
            "final_card_count": compact.get("final_card_count"),
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
