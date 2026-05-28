from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.schemas.component_qa import ComponentRepairResponse
from app.schemas.image_task import ImageTask
from app.services.component_generator import execute_image_tasks
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class RepairLoopError(RuntimeError):
    pass


def _find_task(payload, task_id: str) -> Dict[str, Any] | None:
    for task in payload.image_tasks or []:
        if str(task.get("task_id")) == str(task_id):
            return task
    return None


def _repair_task_from_original(original: Dict[str, Any], repair: Dict[str, Any], retry_count: int) -> Dict[str, Any]:
    repaired = dict(original)
    repaired["task_id"] = f"{original.get('task_id')}_repair_{retry_count}"
    repaired["instruction_for_image_ai"] = (
        str(original.get("instruction_for_image_ai") or "")
        + "\n\nREPAIR INSTRUCTION FROM COMPONENT QA:\n"
        + str(repair.get("repair_instruction") or "Improve the component according to QA problems.")
        + "\n\nQA PROBLEMS:\n"
        + "; ".join(str(p) for p in repair.get("problems", []))
    )
    metadata = dict(repaired.get("metadata") or {})
    metadata["repair_of_task_id"] = original.get("task_id")
    metadata["repair_of_component_id"] = repair.get("component_id")
    metadata["retry_count"] = retry_count
    repaired["metadata"] = metadata
    # Keep the same component_id so the successful repair replaces the old component.
    if repair.get("component_id"):
        repaired["component_ids"] = [repair["component_id"]]
    return repaired




def _qa_score(status: Dict[str, Any], component_id: str) -> float:
    try:
        return float((status.get("component_qa") or {}).get(component_id, {}).get("score") or 0.0)
    except Exception:
        return 0.0


def _snapshot_attempt(status: Dict[str, Any], component_id: str, event: str) -> None:
    record = (status.get("components") or {}).get(component_id, {})
    if not isinstance(record, dict):
        return
    path = record.get("path")
    if not path:
        return
    status.setdefault("retry_history", {}).setdefault(component_id, []).append({
        "event": event,
        "retry_count": int(record.get("retry_count") or 0),
        "score": _qa_score(status, component_id),
        "path": path,
        "task_id": record.get("task_id"),
    })


def _select_best_attempt(status: Dict[str, Any], component_id: str) -> None:
    record = (status.get("components") or {}).get(component_id, {})
    attempts = list((status.get("retry_history") or {}).get(component_id, []))
    current_path = record.get("path") if isinstance(record, dict) else None
    if current_path:
        attempts.append({
            "event": "current_at_max_retries",
            "retry_count": int(record.get("retry_count") or 0),
            "score": _qa_score(status, component_id),
            "path": current_path,
            "task_id": record.get("task_id"),
        })
    valid = [a for a in attempts if a.get("path")]
    if not valid or not isinstance(record, dict):
        return
    best = max(valid, key=lambda a: float(a.get("score") or 0.0))
    record["path"] = best.get("path")
    record["status"] = "generated"
    record["best_version"] = True
    record.setdefault("metadata", {})["best_effort_after_max_retries"] = True
    record.setdefault("metadata", {})["selected_best_attempt"] = best
    status.setdefault("components", {})[component_id] = record


def run_component_repair_loop(db: Session, state_id: int, *, max_retries: int = 3) -> ComponentRepairResponse:
    """Stage 6 repair loop.

    Only components currently marked as needs_repair are regenerated. OK
    components are never rechecked/regenerated here. Each component can be
    repaired at most max_retries times; after that the best available version is
    kept and marked as best_effort.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    status = payload.component_status or {}
    repair_tasks = status.get("repair_tasks") or {}
    if not repair_tasks:
        return ComponentRepairResponse(
            project_state_id=state_id,
            pipeline_stage=state.pipeline_stage,
            state_version=state.state_version,
            repaired_count=0,
            skipped_count=0,
            maxed_out_count=0,
            component_status=status,
            component_qa=status.get("component_qa", {}),
            issues=[],
        )

    original_image_tasks = list(payload.image_tasks or [])
    repair_image_tasks: List[Dict[str, Any]] = []
    skipped = 0
    maxed = 0
    issues: List[str] = []

    for component_id, repair in repair_tasks.items():
        if not isinstance(repair, dict):
            skipped += 1
            continue
        component_record = (status.get("components") or {}).get(component_id, {})
        retry_count = int(component_record.get("retry_count") or repair.get("retry_count") or 0)
        component_max = int(repair.get("max_retries") or max_retries)
        if retry_count >= component_max:
            maxed += 1
            _snapshot_attempt(status, component_id, "max_retries_final_candidate")
            _select_best_attempt(status, component_id)
            issues.append(f"max_retries_reached:{component_id}")
            continue
        metadata = component_record.get("metadata") if isinstance(component_record, dict) else {}
        root_task_id = (
            (metadata or {}).get("repair_of_task_id")
            or (component_record.get("task_contract") or {}).get("task_id")
            or repair.get("task_id")
            or component_record.get("task_id")
        )
        original = _find_task(payload, str(root_task_id))
        if not original:
            skipped += 1
            issues.append(f"original_task_not_found:{component_id}:{root_task_id}")
            continue
        _snapshot_attempt(status, component_id, "before_repair_attempt")
        repair_image_tasks.append(_repair_task_from_original(original, repair, retry_count + 1))
        # update retry count immediately so repeated crashes do not loop forever
        component_record["retry_count"] = retry_count + 1
        status.setdefault("components", {})[component_id] = component_record

    if not repair_image_tasks:
        payload.component_status = status
        new_state = update_project_state(
            db,
            state_id,
            pipeline_stage="component_qa",
            payload=payload,
            stage_result={"stage": "component_repair_loop", "issues": issues},
        )
        return ComponentRepairResponse(
            project_state_id=state_id,
            pipeline_stage=new_state.pipeline_stage,
            state_version=new_state.state_version,
            repaired_count=0,
            skipped_count=skipped,
            maxed_out_count=maxed,
            component_status=status,
            component_qa=status.get("component_qa", {}),
            issues=issues,
        )

    # Temporarily replace image_tasks with repair tasks for execution, then restore.
    payload.image_tasks = repair_image_tasks
    payload.component_status = status
    update_project_state(
        db,
        state_id,
        pipeline_stage="component_qa",
        payload=payload,
        stage_result={"stage": "prepare_repair_tasks", "repair_task_count": len(repair_image_tasks)},
    )
    gen_response = execute_image_tasks(db=db, state_id=state_id, only_failed=False, prepare=False)

    # Restore original image tasks and clear repair tasks that were attempted;
    # next QA run will decide if they are OK or need another attempt.
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    payload.image_tasks = original_image_tasks
    status = payload.component_status or {}
    for task in repair_image_tasks:
        for component_id in task.get("component_ids") or []:
            status.get("repair_tasks", {}).pop(component_id, None)
    payload.component_status = status
    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="component_qa",
        payload=payload,
        stage_result={
            "stage": "component_repair_loop_complete",
            "repaired_count": gen_response.generated_count,
            "skipped_count": skipped,
            "maxed_out_count": maxed,
            "issues": issues + gen_response.issues,
        },
    )

    return ComponentRepairResponse(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        repaired_count=gen_response.generated_count,
        skipped_count=skipped,
        maxed_out_count=maxed,
        component_status=status,
        component_qa=status.get("component_qa", {}),
        issues=issues + gen_response.issues,
    )
