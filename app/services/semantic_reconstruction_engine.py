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
        for key in ("preserve_components", "remove_components", "generate_components"):
            value = entity.get(key)
            if not isinstance(value, list):
                entity[key] = []
                issues.append(f"{key}_missing_or_invalid:{entity_id}")
        if not str(entity.get("semantic_png_description") or "").strip() and entity.get("decision") not in {"remove", "merge"}:
            issues.append(f"semantic_png_description_missing:{entity_id}")
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



def save_semantic_analysis_json(asset_id: int, state_id: int, payload: ProjectStatePayload, raw_normalized: Dict[str, Any], issues: List[str]) -> str:
    analysis_dir = Path("storage/analysis")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"asset-{asset_id}-state-{state_id}-semantic-analysis.json"
    data = {
        "asset_id": asset_id,
        "project_state_id": state_id,
        "pipeline_stage": "semantic_analysis",
        "validation_issues": issues,
        "payload": payload.model_dump(),
        "raw_semantic_analysis": raw_normalized,
    }
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(output_path)


def run_semantic_reconstruction_analysis(db: Session, asset_id: int):
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if asset is None:
        raise SemanticReconstructionError(f"Asset #{asset_id} not found")

    user_prompt = SEMANTIC_RECONSTRUCTION_USER_TEMPLATE.format(
        asset_id=asset.id,
        source_type=asset.source_type or "",
        media_type=asset.media_type or "",
        caption=_cut(asset.caption),
        text_content=_cut(asset.text_content),
        source_url=asset.source_url or "",
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

    payload = ProjectStatePayload(
        analysis_state={
            "asset_type": normalized.get("asset_type"),
            "topic": normalized.get("topic"),
            "source_pattern": normalized.get("source_pattern", {}),
            "medical_editorial_audit": normalized.get("medical_editorial_audit", {}),
            "image_composition_prompt": normalized.get("image_composition_prompt", ""),
            "validation_issues": issues,
        },
        visual_entity_map=normalized.get("visual_entity_map", []),
        semantic_png_plan=normalized.get("semantic_png_plan", []),
        design_blueprint=normalized.get("design_blueprint", {}),
        post=normalized.get("post", {}),
        qa_checklist=normalized.get("qa_checklist", []),
        continuation_package=continuation,
        custom={"raw_semantic_analysis": normalized, "validation_issues": issues},
    )

    state = create_project_state(
        db,
        asset_id=asset_id,
        pipeline_stage="semantic_analysis",
        payload=payload,
    )

    analysis_path = save_semantic_analysis_json(asset_id, state.id, payload, normalized, issues)
    payload.custom["analysis_json_path"] = analysis_path
    payload.analysis_state["analysis_json_path"] = analysis_path
    from app.services.project_state_manager import update_project_state

    state = update_project_state(
        db,
        state.id,
        pipeline_stage="semantic_analysis",
        payload=payload,
        stage_result={"analysis_json_path": analysis_path, "validation_issues": issues},
    )
    return state, issues
