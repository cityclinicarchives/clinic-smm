from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import ProjectState
from app.schemas.project_state import (
    ContinuationPackage,
    PipelineStage,
    ProjectStatePayload,
)


class ProjectStateError(RuntimeError):
    pass


def _to_json(data: Any) -> str:
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    return json.dumps(data, ensure_ascii=False, indent=2)


def _from_json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def create_project_state(
    db: Session,
    *,
    asset_id: Optional[int] = None,
    reconstruction_id: Optional[int] = None,
    pipeline_stage: PipelineStage = "intake",
    payload: Optional[ProjectStatePayload | Dict[str, Any]] = None,
) -> ProjectState:
    """Create persistent state for one reconstruction pipeline run."""

    if payload is None:
        payload_obj = ProjectStatePayload()
    elif isinstance(payload, ProjectStatePayload):
        payload_obj = payload
    else:
        payload_obj = ProjectStatePayload.model_validate(payload)

    state = ProjectState(
        asset_id=asset_id,
        reconstruction_id=reconstruction_id,
        pipeline_stage=pipeline_stage,
        payload_json=_to_json(payload_obj),
        continuation_package_json=_to_json(payload_obj.continuation_package),
        stage_history_json=_to_json([
            {
                "stage": pipeline_stage,
                "event": "created",
                "state_version": 1,
            }
        ]),
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def get_project_state(db: Session, state_id: int) -> ProjectState:
    state = db.query(ProjectState).filter(ProjectState.id == state_id).first()
    if state is None:
        raise ProjectStateError(f"Project state #{state_id} not found")
    return state


def get_payload(state: ProjectState) -> ProjectStatePayload:
    return ProjectStatePayload.model_validate(
        _from_json(state.payload_json, {})
    )


def get_continuation_package(state: ProjectState) -> ContinuationPackage:
    return ContinuationPackage.model_validate(
        _from_json(state.continuation_package_json, {})
    )


def get_stage_history(state: ProjectState) -> List[Dict[str, Any]]:
    history = _from_json(state.stage_history_json, [])
    return history if isinstance(history, list) else []


def update_project_state(
    db: Session,
    state_id: int,
    *,
    pipeline_stage: Optional[PipelineStage] = None,
    payload: Optional[ProjectStatePayload | Dict[str, Any]] = None,
    stage_result: Optional[Dict[str, Any]] = None,
) -> ProjectState:
    """
    Update state atomically. Every update increments state_version and appends history.
    This is the persistent memory bridge between separate API calls.
    """

    state = get_project_state(db, state_id)
    current_payload = get_payload(state)

    if payload is not None:
        if isinstance(payload, ProjectStatePayload):
            payload_obj = payload
        else:
            payload_obj = ProjectStatePayload.model_validate(payload)
    else:
        payload_obj = current_payload

    if pipeline_stage is not None:
        state.pipeline_stage = pipeline_stage
        payload_obj.continuation_package.last_successful_stage = pipeline_stage

    state.state_version = (state.state_version or 0) + 1
    state.payload_json = _to_json(payload_obj)
    state.continuation_package_json = _to_json(payload_obj.continuation_package)

    history = get_stage_history(state)
    history.append(
        {
            "stage": state.pipeline_stage,
            "event": "updated",
            "state_version": state.state_version,
            "result": stage_result or {},
        }
    )
    state.stage_history_json = _to_json(history)

    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def merge_payload_section(
    db: Session,
    state_id: int,
    *,
    section: str,
    value: Any,
    pipeline_stage: Optional[PipelineStage] = None,
    stage_result: Optional[Dict[str, Any]] = None,
) -> ProjectState:
    payload = get_payload(get_project_state(db, state_id))
    if not hasattr(payload, section):
        raise ProjectStateError(f"Unknown payload section: {section}")
    setattr(payload, section, value)
    return update_project_state(
        db,
        state_id,
        pipeline_stage=pipeline_stage,
        payload=payload,
        stage_result=stage_result or {"section": section},
    )


def build_ai_context_from_state(state: ProjectState) -> Dict[str, Any]:
    """
    Context package to send into every AI call after chain breaks.
    Future engines must use this instead of short summaries.
    """

    payload = get_payload(state)
    return {
        "project_state_id": state.id,
        "state_version": state.state_version,
        "pipeline_stage": state.pipeline_stage,
        "analysis_state": payload.analysis_state,
        "semantic_units": payload.semantic_units,
        "unit_decisions": payload.unit_decisions,
        "final_units": payload.final_units,
        "component_map": payload.component_map,
        "layout_blueprint": payload.layout_blueprint,
        "image_tasks": payload.image_tasks,
        "component_status": payload.component_status,
        "layout_versions": payload.layout_versions,
        "render_history": payload.render_history,
        "approved_component_versions": payload.approved_component_versions,
        "post_brief": payload.post_brief,
        "continuation_package": payload.continuation_package.model_dump(),
    }
