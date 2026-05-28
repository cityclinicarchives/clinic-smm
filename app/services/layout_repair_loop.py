from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.schemas.draft_qa import DraftRepairResponse
from app.services.project_state_manager import get_payload, get_project_state, update_project_state
from app.services.technical_renderer import TechnicalRenderError, render_technical_draft


class LayoutRepairError(RuntimeError):
    pass


MAX_LAYOUT_RETRIES = 3


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _latest_draft_qa(payload) -> Dict[str, Any]:
    status = payload.component_status or {}
    item = status.get("latest_draft_qa")
    return item if isinstance(item, dict) else {}


def _repair_history(payload) -> list[dict[str, Any]]:
    status = payload.component_status or {}
    history = status.setdefault("layout_repair_history", [])
    if not isinstance(history, list):
        status["layout_repair_history"] = []
        history = status["layout_repair_history"]
    payload.component_status = status
    return history


def _apply_simple_repairs(layout: Dict[str, Any], repairs: List[Dict[str, Any]], attempt: int) -> Dict[str, Any]:
    new_layout = deepcopy(layout or {})
    canvas = new_layout.setdefault("canvas", {})
    canvas["w"] = max(800, _safe_int(canvas.get("w"), 1080))
    canvas["h"] = max(800, _safe_int(canvas.get("h"), 1350))
    blocks = new_layout.setdefault("blocks", [])
    if not isinstance(blocks, list):
        new_layout["blocks"] = []
        return new_layout

    # Apply explicit repairs when AI gives machine-readable instructions.
    by_id = {str(b.get("block_id") or b.get("component_id") or i): b for i, b in enumerate(blocks) if isinstance(b, dict)}
    for repair in repairs:
        if not isinstance(repair, dict):
            continue
        block_id = str(repair.get("block_id") or repair.get("component_id") or "")
        block = by_id.get(block_id)
        if not block:
            continue
        for key in ("x", "y", "w", "h"):
            if key in repair:
                block[key] = max(0, _safe_int(repair.get(key), _safe_int(block.get(key), 0)))

    # Universal safe fallback adjustment: increase canvas height and add spacing.
    canvas["h"] = int(canvas["h"] * (1.08 + 0.04 * max(0, attempt - 1)))
    margin = 40
    gap = 24 + attempt * 6
    y_cursor = margin
    row: list[dict[str, Any]] = []
    row_h = 0
    x_cursor = margin
    max_w = canvas["w"] - margin
    for block in blocks:
        if not isinstance(block, dict):
            continue
        w = max(80, _safe_int(block.get("w"), 220))
        h = max(80, _safe_int(block.get("h"), 160))
        if x_cursor + w > max_w and row:
            y_cursor += row_h + gap
            x_cursor = margin
            row = []
            row_h = 0
        block["x"] = x_cursor
        block["y"] = y_cursor
        block["w"] = w
        block["h"] = h
        x_cursor += w + gap
        row.append(block)
        row_h = max(row_h, h)
    total_h = y_cursor + row_h + margin
    if total_h > canvas["h"]:
        canvas["h"] = total_h
    notes = new_layout.setdefault("layout_notes", [])
    if isinstance(notes, list):
        notes.append(f"Stage 9 layout repair attempt {attempt}: adjusted canvas/spacing to avoid overflow.")
    return new_layout


def run_layout_repair_loop(db: Session, state_id: int) -> DraftRepairResponse:
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    qa = _latest_draft_qa(payload)
    if not qa:
        raise LayoutRepairError("No draft QA found. Run /draft/qa first.")
    if qa.get("draft_ok"):
        return DraftRepairResponse(
            project_state_id=state_id,
            pipeline_stage=state.pipeline_stage,
            state_version=state.state_version,
            repaired=False,
            selected_best=True,
            render_path=qa.get("render_path"),
            issues=["draft_already_ok"],
            layout_blueprint=payload.layout_blueprint,
        )

    history = _repair_history(payload)
    attempt = len(history) + 1
    if attempt > MAX_LAYOUT_RETRIES:
        best = None
        candidates = [h for h in history if isinstance(h, dict)]
        if candidates:
            best = sorted(candidates, key=lambda h: float(h.get("score") or 0), reverse=True)[0]
        return DraftRepairResponse(
            project_state_id=state_id,
            pipeline_stage=state.pipeline_stage,
            state_version=state.state_version,
            repaired=False,
            attempt=MAX_LAYOUT_RETRIES,
            max_attempts=MAX_LAYOUT_RETRIES,
            selected_best=True,
            render_path=(best or {}).get("render_path"),
            issues=["layout_repair_max_retries_reached"],
            layout_blueprint=payload.layout_blueprint,
        )

    repairs = qa.get("layout_repairs") if isinstance(qa.get("layout_repairs"), list) else []
    payload.layout_blueprint = _apply_simple_repairs(payload.layout_blueprint or {}, repairs, attempt)
    history.append({"attempt": attempt, "input_problems": qa.get("problems") or [], "layout_repairs": repairs, "score": qa.get("score") or 0})
    payload.component_status = payload.component_status or {}
    payload.component_status["layout_repair_history"] = history
    update_project_state(
        db,
        state_id,
        pipeline_stage="draft_qa",
        payload=payload,
        stage_result={"stage": "layout_repair", "attempt": attempt},
    )

    try:
        result = render_technical_draft(db=db, state_id=state_id)
        history[-1]["render_path"] = result.render_path
        history[-1]["issues"] = result.issues
        payload = get_payload(get_project_state(db, state_id))
        payload.component_status = payload.component_status or {}
        payload.component_status["layout_repair_history"] = history
        update_project_state(db, state_id, pipeline_stage="draft_qa", payload=payload, stage_result={"stage": "layout_repair_rendered", "attempt": attempt})

        # IDEAL PIPELINE v2 Stage 9: every repair must be followed immediately
        # by QA of the newly rendered draft. The caller should not have to run
        # /draft/qa manually after /draft/repair. OK components/renders stay in
        # history; only the repaired render is checked here.
        qa_after_repair = None
        qa_issues: list[str] = []
        try:
            from app.services.draft_qa_engine import run_draft_qa

            qa_after_repair = run_draft_qa(db=db, state_id=state_id)
            history[-1]["qa_after_repair"] = qa_after_repair.model_dump()
            payload = get_payload(get_project_state(db, state_id))
            payload.component_status = payload.component_status or {}
            payload.component_status["layout_repair_history"] = history
            update_project_state(
                db,
                state_id,
                pipeline_stage="draft_qa",
                payload=payload,
                stage_result={
                    "stage": "layout_repair_rendered_and_checked",
                    "attempt": attempt,
                    "draft_ok": qa_after_repair.draft_ok,
                    "recommendation": qa_after_repair.recommendation,
                },
            )
            qa_issues = list(qa_after_repair.problems or [])
        except Exception as qa_exc:
            qa_issues = [f"draft_qa_failed_after_repair:{type(qa_exc).__name__}:{str(qa_exc)[:180]}"]

        return DraftRepairResponse(
            project_state_id=state_id,
            pipeline_stage="draft_qa",
            state_version=get_project_state(db, state_id).state_version,
            repaired=True,
            attempt=attempt,
            max_attempts=MAX_LAYOUT_RETRIES,
            render_path=result.render_path,
            issues=list(result.issues or []) + qa_issues,
            layout_blueprint=payload.layout_blueprint,
            qa_after_repair=qa_after_repair,
            stop_reason=("ready_for_polish" if qa_after_repair and qa_after_repair.draft_ok else "needs_next_repair_or_best_selection"),
        )
    except TechnicalRenderError as exc:
        return DraftRepairResponse(
            project_state_id=state_id,
            pipeline_stage="draft_qa",
            state_version=get_project_state(db, state_id).state_version,
            repaired=False,
            attempt=attempt,
            max_attempts=MAX_LAYOUT_RETRIES,
            issues=[f"technical_render_failed_after_repair:{exc}"],
            layout_blueprint=payload.layout_blueprint,
        )
