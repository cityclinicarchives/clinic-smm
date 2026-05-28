from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset, ProjectState
from app.prompts.master_reconstruction import (
    MASTER_RECONSTRUCTION_SYSTEM_PROMPT,
    MASTER_RECONSTRUCTION_USER_TEMPLATE,
)
from app.schemas.project_state import ProjectStatePayload
from app.services.project_state_manager import create_project_state, update_project_state
from app.services.telegram_bot import download_file_bytes


class MasterReconstructionError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise MasterReconstructionError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _cut(text: Optional[str], limit: int = 9000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...обрезано"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MasterReconstructionError(f"Master reconstruction did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MasterReconstructionError("Master reconstruction JSON must be an object.")
    return data




def _dict_list(data: dict[str, Any], key: str, issues: list[str]) -> list[dict[str, Any]]:
    """Return only object items from an AI-produced list and record bad placeholders."""
    raw = data.get(key) or []
    if not isinstance(raw, list):
        issues.append(f"{key}_not_list")
        return []
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            cleaned.append(item)
        else:
            preview = str(item).replace("\n", " ")[:120]
            issues.append(f"{key}.{index}_not_object:{preview}")
    return cleaned


def _dict_value(data: dict[str, Any], key: str, issues: list[str]) -> dict[str, Any]:
    raw = data.get(key) or {}
    if isinstance(raw, dict):
        return raw
    issues.append(f"{key}_not_object")
    return {}

def _asset_or_raise(db: Session, asset_id: int) -> ContentAsset:
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if not asset:
        raise MasterReconstructionError(f"ContentAsset #{asset_id} not found")
    return asset


def _asset_image_content(asset: ContentAsset) -> list[dict[str, Any]]:
    if not (asset.media_file_id and asset.media_type in {"photo", "document"}):
        return []
    try:
        image_bytes = download_file_bytes(asset.media_file_id)
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"}]
    except Exception:
        return []


def _render_user_prompt(asset: ContentAsset, instruction: Optional[str]) -> str:
    # Do not use str.format() here: the template intentionally contains JSON braces.
    # We replace explicit tokens only, so JSON schema examples remain valid.
    replacements = {
        "__ASSET_ID__": str(asset.id),
        "__SOURCE_TYPE__": asset.source_type or "manual",
        "__SOURCE_URL__": asset.source_url or "",
        "__MEDIA_TYPE__": asset.media_type or "",
        "__ASSET_TEXT__": _cut(asset.text_content, 7000),
        "__ASSET_CAPTION__": _cut(asset.caption, 5000),
        "__ASSET_ANALYSIS__": _cut(asset.analysis, 7000),
        "__INSTRUCTION__": instruction or "",
    }
    prompt = MASTER_RECONSTRUCTION_USER_TEMPLATE
    for token, value in replacements.items():
        prompt = prompt.replace(token, value)
    return prompt


def _payload_from_master_json(data: dict[str, Any]) -> ProjectStatePayload:
    normalization_issues: list[str] = []
    payload = ProjectStatePayload()
    payload.analysis_state = _dict_value(data, "analysis_state", normalization_issues)
    payload.semantic_units = _dict_list(data, "semantic_units", normalization_issues)
    payload.unit_decisions = _dict_list(data, "unit_decisions", normalization_issues)
    payload.final_units = _dict_list(data, "final_units", normalization_issues)
    payload.component_map = _dict_list(data, "component_map", normalization_issues)
    payload.layout_blueprint = _dict_value(data, "layout_blueprint", normalization_issues)
    payload.image_tasks = _dict_list(data, "image_tasks", normalization_issues)
    payload.post_brief = _dict_value(data, "post_brief", normalization_issues)
    if normalization_issues:
        payload.analysis_state.setdefault("normalization_issues", []).extend(normalization_issues)

    continuation = data.get("continuation_package") or {}
    if isinstance(continuation, dict):
        payload.continuation_package.current_state_summary = continuation.get("current_state_summary") or ""
        payload.continuation_package.strict_contract = continuation.get("strict_contract") or {}
        payload.continuation_package.must_not_forget = continuation.get("must_not_forget") or []
        payload.continuation_package.next_step_prompt = continuation.get("next_step_prompt") or ""
        payload.continuation_package.last_successful_stage = "master_reconstruction"
    elif continuation:
        payload.analysis_state.setdefault("normalization_issues", []).append("continuation_package_not_object")
    return payload


def validate_master_payload(payload: ProjectStatePayload) -> list[str]:
    """Validate stages 1-3 of the new stateful pipeline.

    This validator does not judge final design quality yet. It only enforces the
    contract that must exist before later image-task stages can safely run:
    persistent state, one explicit source-unit decision per source unit, final
    units built from decisions, replacement reference units, and complete
    continuation memory.
    """
    issues: list[str] = []
    if not payload.analysis_state:
        issues.append("missing_analysis_state")
    if not payload.semantic_units:
        issues.append("missing_semantic_units")
    if not payload.unit_decisions:
        issues.append("missing_unit_decisions")
    if not payload.final_units:
        issues.append("missing_final_units")
    if not payload.component_map:
        issues.append("missing_component_map")
    if not payload.image_tasks:
        issues.append("missing_image_tasks")
    if not payload.layout_blueprint:
        issues.append("missing_layout_blueprint")
    if not payload.post_brief:
        issues.append("missing_post_brief")
    if not payload.continuation_package.current_state_summary:
        issues.append("missing_continuation_summary")
    if not payload.continuation_package.strict_contract:
        issues.append("missing_strict_contract")
    if not payload.continuation_package.next_step_prompt:
        issues.append("missing_next_step_prompt")

    allowed = {"keep", "remove", "replace", "merge"}

    source_ids = {
        str(unit.get("source_unit_id") or unit.get("id") or unit.get("unit_id"))
        for unit in payload.semantic_units
        if isinstance(unit, dict) and (unit.get("source_unit_id") or unit.get("id") or unit.get("unit_id"))
    }
    decision_source_ids = [
        str(dec.get("source_unit_id") or dec.get("source_id"))
        for dec in payload.unit_decisions
        if isinstance(dec, dict) and (dec.get("source_unit_id") or dec.get("source_id"))
    ]
    decision_ids = set(decision_source_ids)

    missing_decisions = sorted(source_ids - decision_ids)
    if missing_decisions:
        issues.append(f"source_units_without_decision:{','.join(missing_decisions[:10])}")

    duplicate_decisions = sorted({sid for sid in decision_source_ids if decision_source_ids.count(sid) > 1})
    if duplicate_decisions:
        issues.append(f"duplicate_source_unit_decisions:{','.join(duplicate_decisions[:10])}")

    final_unit_ids = {
        str(unit.get("final_unit_id") or unit.get("id") or unit.get("unit_id"))
        for unit in payload.final_units
        if isinstance(unit, dict) and (unit.get("final_unit_id") or unit.get("id") or unit.get("unit_id"))
    }
    represented_sources = {
        str(sid)
        for unit in payload.final_units
        if isinstance(unit, dict)
        for sid in (unit.get("source_unit_ids") or unit.get("source_ids") or [])
    }

    removed_sources: set[str] = set()
    for dec in payload.unit_decisions:
        if not isinstance(dec, dict):
            continue
        sid = str(dec.get("source_unit_id") or dec.get("source_id") or "")
        decision = str(dec.get("decision") or "").strip().lower()
        if decision not in allowed:
            issues.append(f"invalid_decision:{sid or dec.get('source_title')}")
            continue
        if decision == "remove":
            removed_sources.add(sid)
            if dec.get("has_good_alternative") is True:
                issues.append(f"remove_but_has_good_alternative:{sid}")
        if decision == "replace":
            if not (dec.get("replacement_title") or dec.get("final_unit_id") or dec.get("final_title")):
                issues.append(f"replace_without_replacement_title:{sid}")
            if not dec.get("reference_unit_id"):
                issues.append(f"replace_without_reference_unit:{sid}")
            if not dec.get("style_inheritance_rules"):
                issues.append(f"replace_without_style_inheritance_rules:{sid}")
        if decision == "merge" and not (dec.get("merge_target_unit_id") or dec.get("final_unit_id") or dec.get("final_title")):
            issues.append(f"merge_without_target:{sid}")
        if decision in {"keep", "merge", "replace"}:
            target = str(dec.get("final_unit_id") or dec.get("merge_target_unit_id") or "")
            if sid and sid not in represented_sources and target and target not in final_unit_ids:
                issues.append(f"decision_not_represented_in_final_units:{sid}")

    for unit in payload.final_units:
        if not isinstance(unit, dict):
            continue
        uid = unit.get("final_unit_id") or unit.get("id") or unit.get("unit_id")
        if not uid:
            issues.append("final_unit_without_id")
        if not (unit.get("title") or unit.get("label") or unit.get("text")):
            issues.append(f"final_unit_without_title:{uid}")
        policy = unit.get("source_policy") or unit.get("unit_type") or unit.get("generation_policy")
        if policy == "replace_with_new":
            if not unit.get("reference_unit_id"):
                issues.append(f"final_replace_without_reference_unit:{uid}")
            if not unit.get("style_inheritance_rules"):
                issues.append(f"final_replace_without_style_rules:{uid}")
        for sid in (unit.get("source_unit_ids") or unit.get("source_ids") or []):
            if str(sid) in removed_sources:
                issues.append(f"removed_source_used_in_final_units:{sid}")

    task_unit_ids = {
        str(task.get("final_unit_id") or task.get("unit_id") or task.get("component_id"))
        for task in payload.image_tasks
        if isinstance(task, dict) and (task.get("final_unit_id") or task.get("unit_id") or task.get("component_id"))
    }
    missing_tasks = sorted(uid for uid in final_unit_ids if uid and uid not in task_unit_ids)
    if missing_tasks:
        issues.append(f"final_units_without_image_task:{','.join(missing_tasks[:10])}")

    return issues


def run_master_reconstruction(
    db: Session,
    *,
    asset_id: int,
    instruction: Optional[str] = None,
) -> ProjectState:
    """
    API call #1 from the new pipeline.
    Performs the full analytical planning call and stores ProjectState.
    """
    asset = _asset_or_raise(db, asset_id)
    client = _get_client()
    user_prompt = _render_user_prompt(asset, instruction)
    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
    user_content.extend(_asset_image_content(asset))

    response = client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": MASTER_RECONSTRUCTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    raw = response.output_text.strip()
    data = _extract_json(raw)
    payload = _payload_from_master_json(data)
    issues = validate_master_payload(payload)
    payload.analysis_state.setdefault("master_validation_issues", issues)
    payload.continuation_package.strict_contract.setdefault("master_validation_issues", issues)

    state = create_project_state(
        db,
        asset_id=asset.id,
        pipeline_stage="master_reconstruction",
        payload=payload,
    )
    # ensure history records validation status explicitly
    state = update_project_state(
        db,
        state.id,
        pipeline_stage="master_reconstruction",
        payload=payload,
        stage_result={"master_validation_issues": issues, "raw_response_chars": len(raw)},
    )
    return state
