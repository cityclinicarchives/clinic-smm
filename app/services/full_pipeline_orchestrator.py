from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.master_reconstruction_engine import run_master_reconstruction
from app.services.image_task_engine import prepare_image_tasks
from app.services.component_generator import execute_image_tasks
from app.services.component_qa_engine import run_component_qa
from app.services.repair_loop import run_component_repair_loop
from app.services.final_layout_engine import finalize_layout
from app.services.technical_renderer import render_technical_draft
from app.services.draft_qa_engine import run_draft_qa
from app.services.layout_repair_loop import run_layout_repair_loop
from app.services.design_polish_engine import DesignPolishError, run_design_polish
from app.services.final_qa_engine import run_final_qa
from app.services.reconstruction_post_writer import generate_post_from_reconstruction_state
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class FullPipelineError(RuntimeError):
    pass


def _dump(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    data: Dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if isinstance(value, (str, int, float, bool, list, dict, type(None))):
            data[name] = value
    return data


def _step(name: str, status: str, result: Any = None, issues: Optional[List[str]] = None) -> Dict[str, Any]:
    result_data = _dump(result)
    if issues is None:
        raw_issues = result_data.get("issues") or result_data.get("validation_issues") or result_data.get("problems") or []
        issues = [str(x) for x in raw_issues] if isinstance(raw_issues, list) else ([str(raw_issues)] if raw_issues else [])
    return {"step": name, "status": status, "result": result_data, "issues": issues}


def _latest_component_qa_counts(qa: Any) -> tuple[int, int, int]:
    data = _dump(qa)
    return (
        int(data.get("ok_count") or 0),
        int(data.get("needs_repair_count") or 0),
        int(data.get("failed_count") or 0),
    )


def run_full_reconstruction_pipeline(
    db: Session,
    *,
    asset_id: int,
    instruction: str | None = None,
    platform: str = "telegram",
    max_component_repair_attempts: int = 3,
    max_layout_repair_attempts: int = 3,
    run_polish: bool = True,
    generate_post: bool = True,
) -> Dict[str, Any]:
    """Run IDEAL SEMANTIC-LAYOUT RECONSTRUCTION PIPELINE v2 end-to-end.

    This orchestrator is intentionally synchronous for MVP/testing. It uses
    ProjectState as the only persistent memory between AI chain breaks and calls
    the stage engines in the same order as the pipeline contract.
    """
    steps: List[Dict[str, Any]] = []
    state_id: int | None = None

    try:
        # 1. Master analytical reconstruction call.
        state = run_master_reconstruction(db=db, asset_id=asset_id, instruction=instruction)
        state_id = int(state.id)
        steps.append(_step("master_reconstruction", "ok", {
            "project_state_id": state_id,
            "pipeline_stage": state.pipeline_stage,
            "state_version": state.state_version,
        }))

        # 2. Prepare image tasks from the saved master state.
        task_plan = prepare_image_tasks(db=db, state_id=state_id)
        if not getattr(task_plan, "ready", False):
            steps.append(_step("image_tasks_prepare", "failed", task_plan))
            raise FullPipelineError("Image task preparation failed. See validation_issues.")
        steps.append(_step("image_tasks_prepare", "ok", task_plan))

        # 3. Execute image tasks and save PNG components.
        generation = execute_image_tasks(db=db, state_id=state_id, only_failed=False, prepare=False)
        steps.append(_step("image_tasks_execute", "ok", generation))

        # 4. Component QA + repair loop.
        qa = run_component_qa(db=db, state_id=state_id, only_new_or_repaired=True)
        steps.append(_step("component_qa_initial", "ok", qa))
        _ok, needs_repair, _failed = _latest_component_qa_counts(qa)
        repair_attempt = 0
        while needs_repair > 0 and repair_attempt < max_component_repair_attempts:
            repair_attempt += 1
            repair = run_component_repair_loop(db=db, state_id=state_id, max_retries=max_component_repair_attempts)
            steps.append(_step(f"component_repair_{repair_attempt}", "ok", repair))
            qa = run_component_qa(db=db, state_id=state_id, only_new_or_repaired=True)
            steps.append(_step(f"component_qa_after_repair_{repair_attempt}", "ok", qa))
            _ok, needs_repair, _failed = _latest_component_qa_counts(qa)

        if needs_repair > 0:
            # Give the repair loop one last chance to mark/select best-effort versions at max retries.
            repair = run_component_repair_loop(db=db, state_id=state_id, max_retries=max_component_repair_attempts)
            steps.append(_step("component_repair_best_effort", "warning", repair))
            qa = run_component_qa(db=db, state_id=state_id, only_new_or_repaired=True)
            steps.append(_step("component_qa_best_effort", "warning", qa))

        # 5. Final layout refinement.
        layout = finalize_layout(db=db, state_id=state_id)
        if not getattr(layout, "ready", False):
            steps.append(_step("layout_finalize", "warning", layout))
        else:
            steps.append(_step("layout_finalize", "ok", layout))

        # 6. Technical render from approved PNG components.
        render = render_technical_draft(db=db, state_id=state_id)
        if not getattr(render, "ready", False):
            steps.append(_step("technical_render", "failed", render))
            raise FullPipelineError("Technical render failed. See issues.")
        steps.append(_step("technical_render", "ok", render))

        # 7. Draft QA + layout repair loop.
        draft_qa = run_draft_qa(db=db, state_id=state_id)
        steps.append(_step("draft_qa_initial", "ok" if draft_qa.draft_ok else "needs_repair", draft_qa))
        layout_attempt = 0
        while (not draft_qa.draft_ok) and draft_qa.recommendation == "repair_layout" and layout_attempt < max_layout_repair_attempts:
            layout_attempt += 1
            repair = run_layout_repair_loop(db=db, state_id=state_id)
            steps.append(_step(f"layout_repair_{layout_attempt}", "ok" if repair.repaired else "warning", repair))
            # repair loop runs QA internally, but run a fresh explicit QA so the latest state is canonical.
            draft_qa = run_draft_qa(db=db, state_id=state_id)
            steps.append(_step(f"draft_qa_after_layout_repair_{layout_attempt}", "ok" if draft_qa.draft_ok else "needs_repair", draft_qa))

        if not draft_qa.draft_ok and draft_qa.recommendation == "failed":
            raise FullPipelineError("Draft QA failed and cannot be repaired automatically.")

        # 8. Optional design polish. If it fails, Final QA will fall back to technical draft.
        if run_polish and draft_qa.draft_ok:
            try:
                polish = run_design_polish(db=db, state_id=state_id)
                steps.append(_step("design_polish", "ok" if polish.status == "ready" else "warning", polish))
            except DesignPolishError as exc:
                steps.append(_step("design_polish", "skipped_or_failed", {"error": str(exc)}, [str(exc)]))
        elif run_polish:
            steps.append(_step("design_polish", "skipped", {"reason": "draft_qa_not_ok"}))

        # 9. Final QA chooses polished image or technical draft.
        final_qa = run_final_qa(db=db, state_id=state_id)
        steps.append(_step("final_qa", "ok" if final_qa.final_ok else "failed", final_qa))
        if not final_qa.final_ok:
            raise FullPipelineError("Final QA failed. No safe final image selected.")

        post_result = None
        if generate_post:
            post_result = generate_post_from_reconstruction_state(db=db, state_id=state_id, platform=platform)
            steps.append(_step("post_generation", "ok", post_result))

        state = get_project_state(db, state_id)
        payload = get_payload(state)
        payload.component_status.setdefault("full_pipeline_runs", []).append({
            "status": "completed",
            "steps": steps,
            "platform": platform,
        })
        payload.continuation_package.current_state_summary = "Full reconstruction pipeline completed automatically."
        payload.continuation_package.next_step_prompt = "Next step: review/publish the generated post and selected final image."
        state = update_project_state(
            db,
            state_id,
            pipeline_stage="completed",
            payload=payload,
            stage_result={"stage": "full_pipeline", "status": "completed", "step_count": len(steps)},
        )
        return {
            "project_state_id": state_id,
            "pipeline_stage": state.pipeline_stage,
            "state_version": state.state_version,
            "status": "completed",
            "steps": steps,
            "final_image_path": _dump(final_qa).get("final_image_path"),
            "post_id": _dump(post_result).get("post_id") if post_result else None,
            "post_title": _dump(post_result).get("post_title") if post_result else "",
            "post_text": _dump(post_result).get("post_text") if post_result else "",
        }

    except Exception as exc:
        if state_id is not None:
            try:
                state = get_project_state(db, state_id)
                payload = get_payload(state)
                payload.component_status.setdefault("full_pipeline_runs", []).append({
                    "status": "failed",
                    "error": str(exc),
                    "steps": steps,
                })
                payload.continuation_package.current_state_summary = f"Full pipeline failed: {exc}"
                update_project_state(
                    db,
                    state_id,
                    pipeline_stage="failed",
                    payload=payload,
                    stage_result={"stage": "full_pipeline", "status": "failed", "error": str(exc)},
                )
            except Exception:
                pass
        if isinstance(exc, FullPipelineError):
            raise
        raise FullPipelineError(str(exc)) from exc
