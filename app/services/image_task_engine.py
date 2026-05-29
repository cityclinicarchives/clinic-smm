from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

from sqlalchemy.orm import Session

from app.models import ProjectState
from app.schemas.image_task import ImageTask, ImageTaskPlan, PngSize
from app.schemas.project_state import ProjectStatePayload
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class ImageTaskError(RuntimeError):
    pass


DEFAULT_SIZE = {"w": 512, "h": 512}
TEXT_SIZE = {"w": 900, "h": 260}
BACKGROUND_SIZE = {"w": 1080, "h": 1350}


def _unit_id(unit: Dict[str, Any]) -> str:
    return str(unit.get("final_unit_id") or unit.get("unit_id") or unit.get("id") or "").strip()


def _unit_title(unit: Dict[str, Any]) -> str:
    return str(unit.get("label_ru") or unit.get("title") or unit.get("label") or unit.get("text_content") or _unit_id(unit)).strip()


def _is_text_unit(unit: Dict[str, Any]) -> bool:
    unit_type = str(unit.get("unit_type") or unit.get("type") or unit.get("block_type") or "").lower()
    operation = str(unit.get("image_task_operation") or unit.get("operation") or "").lower()
    return (
        unit_type in {"text_png_block", "text_block", "title_block", "warning_block", "cta_block", "footer_text"}
        or operation == "generate_text_png_block"
        or bool(unit.get("text_content"))
    )


def _decision_reference_for_unit(payload: ProjectStatePayload, final_unit_id: str) -> str:
    for decision in payload.unit_decisions or []:
        if not isinstance(decision, dict):
            continue
        if str(decision.get("final_unit") or decision.get("final_unit_id") or "") != final_unit_id:
            continue
        ref = decision.get("reference_unit_id") or decision.get("reference_unit") or decision.get("style_reference_unit")
        if ref:
            return str(ref)
    return ""


def _reference_for_unit(payload: ProjectStatePayload, unit: Dict[str, Any], final_unit_id: str) -> str:
    return str(
        unit.get("reference_unit_id")
        or unit.get("style_reference_unit")
        or unit.get("reference_component_id")
        or _decision_reference_for_unit(payload, final_unit_id)
        or ""
    ).strip()


def _walk_layout_blocks(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        keys = {str(k).lower() for k in value.keys()}
        if {"id", "type"}.intersection(keys) or {"block_id", "text"}.intersection(keys):
            yield value
        for child in value.values():
            yield from _walk_layout_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_layout_blocks(child)


def _ensure_text_units_from_layout(payload: ProjectStatePayload) -> List[str]:
    """Promote layout text blocks to final_units so they get PNG tasks.

    Pipeline v2 requires every textual block (title, warning, CTA, footer, etc.)
    to be generated as a PNG component before Python renders the draft. This
    helper ensures text blocks mentioned only in layout_blueprint are not lost.
    """
    added: List[str] = []
    existing: Set[str] = {_unit_id(u) for u in payload.final_units if isinstance(u, dict) and _unit_id(u)}
    for block in _walk_layout_blocks(payload.layout_blueprint or {}):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or block.get("unit_type") or block.get("block_type") or "").lower()
        text = block.get("text") or block.get("text_content") or block.get("title") or block.get("label")
        looks_text = (
            "text" in block_type
            or "title" in block_type
            or "warning" in block_type
            or "cta" in block_type
            or "footer" in block_type
            or bool(text)
        )
        if not looks_text:
            continue
        fid = str(block.get("final_unit_id") or block.get("unit_id") or block.get("block_id") or block.get("id") or "").strip()
        if not fid:
            fid = f"text_block_{len(existing) + len(added) + 1}"
        if fid in existing:
            continue
        size = block.get("target_png_size") or block.get("size") or block.get("layout") or {}
        w = int(size.get("w") or size.get("width") or TEXT_SIZE["w"]) if isinstance(size, dict) else TEXT_SIZE["w"]
        h = int(size.get("h") or size.get("height") or TEXT_SIZE["h"]) if isinstance(size, dict) else TEXT_SIZE["h"]
        payload.final_units.append({
            "final_unit_id": fid,
            "unit_type": "text_png_block",
            "image_task_operation": "generate_text_png_block",
            "title": str(block.get("title") or block.get("label") or fid),
            "text_content": str(text or block.get("title") or block.get("label") or ""),
            "typography": block.get("typography") or block.get("style") or {},
            "target_png_size": {"w": max(w, 300), "h": max(h, 120)},
            "source": "layout_blueprint_text_block",
        })
        existing.add(fid)
        added.append(fid)
    return added


def _component_ids_for_unit(payload: ProjectStatePayload, final_unit_id: str) -> List[str]:
    ids: List[str] = []
    for item in payload.component_map:
        if not isinstance(item, dict):
            continue
        if str(item.get("final_unit_id") or "") == final_unit_id:
            cid = item.get("component_id") or item.get("id")
            if cid:
                ids.append(str(cid))
    return ids


def _operation_for_unit(unit: Dict[str, Any], component_map_items: List[Dict[str, Any]]) -> str:
    # Text units are special in pipeline v2: every text block must be rendered
    # by Image AI as its own PNG component. Even if the analytical model
    # accidentally set operation=extract_component/generate_replacement_unit,
    # force generate_text_png_block to avoid text_units_without_png_task.
    if _is_text_unit(unit):
        return "generate_text_png_block"

    explicit = str(unit.get("image_task_operation") or unit.get("operation") or "").strip()
    if explicit:
        return explicit

    unit_type = str(unit.get("unit_type") or "").strip()
    policy = str(unit.get("source_decision") or unit.get("source_policy") or unit.get("generation_policy") or "").strip()

    actions = {str(item.get("action") or "") for item in component_map_items if isinstance(item, dict)}
    if "generate_text_png_block" in actions or unit_type == "text_png_block":
        return "generate_text_png_block"
    if "generate_icon" in actions or unit_type == "icon_png":
        return "generate_icon"
    if "generate_background" in actions or unit_type == "background_png":
        return "generate_background"
    if policy in {"replace", "replace_with_new", "generated_new"} or actions.intersection({"generate_replacement", "generate_replacement_unit", "replace_with_new"}):
        return "generate_replacement_unit"
    if policy in {"keep", "merge", "preserve", "use_reference_and_clean"} or actions.intersection({"extract_from_source", "extract_component", "preserve_component", "use_reference_and_clean"}):
        return "extract_component"
    return "generate_replacement_unit"


def _size_for_unit(unit: Dict[str, Any], operation: str) -> Dict[str, int]:
    raw = unit.get("target_png_size") or unit.get("output_png_size") or {}
    if isinstance(raw, dict) and raw.get("w") and raw.get("h"):
        return {"w": int(raw["w"]), "h": int(raw["h"])}
    if operation == "generate_text_png_block":
        return dict(TEXT_SIZE)
    if operation == "generate_background":
        return dict(BACKGROUND_SIZE)
    return dict(DEFAULT_SIZE)




def _fallback_reference_for_unit(payload: ProjectStatePayload, final_unit_id: str) -> str:
    """Pick a safe style-reference unit when the analytical model forgot one.

    This is a fallback, not a business rule. It chooses an already-planned
    non-text visual unit so replacement generation can still inherit style.
    """
    # Prefer explicit keep/merge decisions because they should exist as reusable visuals.
    for decision in payload.unit_decisions or []:
        if not isinstance(decision, dict):
            continue
        decision_type = str(decision.get("decision") or "").lower()
        if decision_type not in {"keep", "merge"}:
            continue
        candidate = str(decision.get("final_unit") or decision.get("final_unit_id") or decision.get("source_unit") or "").strip()
        if candidate and candidate != final_unit_id:
            return candidate
    # Then use any non-text final unit.
    for unit in payload.final_units or []:
        if not isinstance(unit, dict):
            continue
        candidate = _unit_id(unit)
        if candidate and candidate != final_unit_id and not _is_text_unit(unit):
            return candidate
    return ""


def _component_map_for_unit(payload: ProjectStatePayload, final_unit_id: str) -> List[Dict[str, Any]]:
    return [
        item for item in payload.component_map
        if isinstance(item, dict) and str(item.get("final_unit_id") or "") == final_unit_id
    ]


def _build_instruction(unit: Dict[str, Any], operation: str, component_items: List[Dict[str, Any]]) -> str:
    title = _unit_title(unit)
    must_include = []
    must_exclude = []
    for item in component_items:
        must_include.extend(item.get("must_include") or [])
        must_exclude.extend(item.get("must_exclude") or [])
    if operation == "extract_component":
        return (
            f"Create one clean PNG component for final unit '{title}'. "
            "Extract only useful visual components from the source image. "
            "Preserve the intended visual evidence/objects. Remove old text, labels, UI, watermarks, and irrelevant background. "
            f"Must include: {must_include or unit.get('required_components') or []}. "
            f"Must exclude: {must_exclude or ['old labels', 'watermark', 'UI', 'irrelevant background']}"
        )
    if operation == "generate_replacement_unit":
        return (
            f"Generate one replacement PNG component for final unit '{title}'. "
            "Use the reference component/unit style if provided. Match scale, palette, illustration style, lighting, and visual hierarchy. "
            "Keep medical accuracy and do not copy forbidden source objects. "
            f"Visual requirements: {unit.get('visual_requirements') or []}. Medical requirements: {unit.get('medical_requirements') or []}."
        )
    if operation == "generate_text_png_block":
        return (
            f"Generate one finished Cyrillic-safe PNG text block for '{title}'. "
            "The block must include all text, typography, icons, highlights, padding and decoration. "
            "Text must be readable, not cropped, and fit inside output size. "
            f"Text content: {unit.get('text_content') or title}. Typography: {unit.get('typography') or {}}"
        )
    if operation == "generate_background":
        return f"Generate one clean background PNG component for the infographic style: {unit.get('visual_requirements') or title}."
    return f"Generate one clean PNG component for '{title}'."


def _normalize_existing_task(raw: Dict[str, Any], payload: ProjectStatePayload) -> Tuple[ImageTask | None, List[str]]:
    issues: List[str] = []
    task = dict(raw)
    fid = str(task.get("final_unit_id") or task.get("unit_id") or task.get("component_id") or "").strip()
    unit = next((u for u in payload.final_units if isinstance(u, dict) and _unit_id(u) == fid), {})
    component_items = _component_map_for_unit(payload, fid)

    if fid:
        task["final_unit_id"] = fid
    if not task.get("operation"):
        task["operation"] = _operation_for_unit(unit, component_items)
    if "output_png_size" not in task or not isinstance(task.get("output_png_size"), dict):
        task["output_png_size"] = _size_for_unit(unit, str(task.get("operation") or ""))
    if not task.get("task_id") and fid:
        task["task_id"] = f"task_{fid}"
    if not task.get("component_ids"):
        task["component_ids"] = _component_ids_for_unit(payload, fid)
    if not task.get("reference_component_ids"):
        ref = task.get("reference_unit_id") or _reference_for_unit(payload, unit, fid) or _fallback_reference_for_unit(payload, fid)
        task["reference_component_ids"] = [str(ref)] if ref else []
    if not task.get("instruction_for_image_ai"):
        task["instruction_for_image_ai"] = _build_instruction(unit, str(task.get("operation") or ""), component_items)
    if not task.get("must_include"):
        include: List[str] = []
        include.extend(unit.get("required_components") or [])
        for item in component_items:
            include.extend(item.get("must_include") or [])
        task["must_include"] = include
    if not task.get("must_exclude"):
        exclude: List[str] = ["old labels", "watermark", "UI", "irrelevant background"]
        for item in component_items:
            exclude.extend(item.get("must_exclude") or [])
        # preserve order while removing duplicates
        task["must_exclude"] = list(dict.fromkeys(str(x) for x in exclude if x))
    if not task.get("qa_criteria"):
        task["qa_criteria"] = [
            "matches final unit and component role",
            "no old labels or watermark",
            "no irrelevant source background",
            "component is reusable as PNG",
        ]
    task["max_retries"] = 3
    task.setdefault("metadata", {})
    ref_for_metadata = _reference_for_unit(payload, unit, fid) or _fallback_reference_for_unit(payload, fid)
    if ref_for_metadata:
        task["metadata"].setdefault("reference_unit_id", ref_for_metadata)
    try:
        normalized = ImageTask.model_validate(task)
        return normalized, issues
    except Exception as exc:
        issues.append(f"invalid_image_task:{task.get('task_id') or fid}:{exc}")
        return None, issues


def _generate_missing_tasks(payload: ProjectStatePayload, existing: Iterable[ImageTask]) -> List[ImageTask]:
    existing_list = list(existing)
    existing_units = {task.final_unit_id for task in existing_list}
    existing_text_units = {task.final_unit_id for task in existing_list if task.operation == "generate_text_png_block"}
    tasks: List[ImageTask] = []
    for unit in payload.final_units:
        if not isinstance(unit, dict):
            continue
        fid = _unit_id(unit)
        if not fid:
            continue
        is_text = _is_text_unit(unit)
        # If a text final_unit already has a non-text task, that does NOT satisfy
        # the contract. Add a missing generate_text_png_block task for it.
        if (not is_text and fid in existing_units) or (is_text and fid in existing_text_units):
            continue
        component_items = _component_map_for_unit(payload, fid)
        operation = "generate_text_png_block" if is_text else _operation_for_unit(unit, component_items)
        size = _size_for_unit(unit, operation)
        ref_ids: List[str] = []
        ref = _reference_for_unit(payload, unit, fid) or _fallback_reference_for_unit(payload, fid)
        if ref:
            ref_ids.append(str(ref))
        task = ImageTask(
            task_id=(f"text_task_{fid}" if operation == "generate_text_png_block" and fid in existing_units else f"task_{fid}"),
            operation=operation,  # type: ignore[arg-type]
            final_unit_id=fid,
            component_ids=_component_ids_for_unit(payload, fid),
            source_image_required=operation == "extract_component",
            reference_component_ids=ref_ids,
            instruction_for_image_ai=_build_instruction(unit, operation, component_items),
            must_include=list(unit.get("required_components") or ([unit.get("text_content") or _unit_title(unit)] if operation == "generate_text_png_block" else [])),
            must_exclude=["old labels", "watermark", "UI", "irrelevant background"],
            output_png_size=PngSize(**size),
            transparent_or_neutral_background=True,
            max_retries=3,
            qa_criteria=[
                "matches final unit and component role",
                "no old labels or watermark",
                "no irrelevant source background",
                "component is reusable as PNG",
            ],
        )
        tasks.append(task)
    return tasks


def validate_image_tasks(payload: ProjectStatePayload, tasks: List[ImageTask]) -> List[str]:
    issues: List[str] = []
    final_unit_ids = {
        _unit_id(unit) for unit in payload.final_units if isinstance(unit, dict) and _unit_id(unit)
    }
    if not final_unit_ids:
        issues.append("missing_final_units")
    task_ids = [task.task_id for task in tasks]
    duplicates = sorted({tid for tid in task_ids if task_ids.count(tid) > 1})
    if duplicates:
        issues.append("duplicate_task_ids:" + ",".join(duplicates[:10]))

    task_unit_ids = {task.final_unit_id for task in tasks}
    missing = sorted(final_unit_ids - task_unit_ids)
    if missing:
        issues.append("final_units_without_image_task:" + ",".join(missing[:10]))

    unknown = sorted(task_unit_ids - final_unit_ids)
    if unknown:
        issues.append("tasks_for_unknown_final_units:" + ",".join(unknown[:10]))

    for task in tasks:
        if task.max_retries != 3:
            issues.append(f"task_retry_limit_not_3:{task.task_id}")
        if task.operation == "generate_replacement_unit" and not task.reference_component_ids:
            issues.append(f"replacement_task_without_reference_component:{task.task_id}")
        if task.operation == "generate_text_png_block":
            if not task.must_include and not task.instruction_for_image_ai:
                issues.append(f"text_png_block_without_text_contract:{task.task_id}")
        if task.operation == "generate_text_png_block":
            if task.output_png_size.w < 300 or task.output_png_size.h < 120:
                issues.append(f"text_png_block_too_small:{task.task_id}")
        if task.operation == "extract_component" and not task.source_image_required:
            issues.append(f"extract_task_without_source_image:{task.task_id}")

    text_unit_ids = {_unit_id(unit) for unit in payload.final_units if isinstance(unit, dict) and _is_text_unit(unit) and _unit_id(unit)}
    text_task_unit_ids = {task.final_unit_id for task in tasks if task.operation == "generate_text_png_block"}
    missing_text_tasks = sorted(text_unit_ids - text_task_unit_ids)
    if missing_text_tasks:
        issues.append("text_units_without_png_task:" + ",".join(missing_text_tasks[:10]))
    return issues




def _is_blocking_issue(issue: str) -> bool:
    """Some issues are normalization warnings after fallback auto-repair.

    The pipeline should not stop only because the LLM returned a placeholder
    string or forgot a reference, as long as executable tasks now cover final_units.
    """
    non_blocking_prefixes = (
        "text_units_added_from_layout:",
        "image_task_not_object",
        "invalid_image_task:",
        "replacement_task_without_reference_component:",
    )
    return not str(issue).startswith(non_blocking_prefixes)


def prepare_image_tasks(db: Session, state_id: int) -> ImageTaskPlan:
    """Stage 4: normalize and validate atomic Image AI task system.

    This stage does not call Image AI yet. It turns the master analytical state
    into a strict list of executable PNG-component contracts.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    normalized: List[ImageTask] = []
    issues: List[str] = []
    added_text_units = _ensure_text_units_from_layout(payload)
    if added_text_units:
        issues.append("text_units_added_from_layout:" + ",".join(added_text_units[:10]))

    for raw in payload.image_tasks:
        if not isinstance(raw, dict):
            issues.append("image_task_not_object")
            continue
        task, task_issues = _normalize_existing_task(raw, payload)
        issues.extend(task_issues)
        if task is not None:
            normalized.append(task)

    generated = _generate_missing_tasks(payload, normalized)
    normalized.extend(generated)
    issues.extend(validate_image_tasks(payload, normalized))

    payload.image_tasks = [task.model_dump() for task in normalized]
    payload.component_status.setdefault("image_tasks", {})
    payload.component_status["image_tasks"].update(
        {
            "prepared": True,
            "ready": not any(_is_blocking_issue(i) for i in issues),
            "validation_issues": issues,
            "task_count": len(normalized),
        }
    )
    payload.continuation_package.last_successful_stage = "image_tasks"
    payload.continuation_package.strict_contract.setdefault("image_task_rules", [])
    payload.continuation_package.strict_contract["image_task_rules"] = [
        "Each image_task must produce exactly one reusable PNG component.",
        "Text blocks must be generated as PNG components; Python must not draw text in later render stages.",
        "Every final_unit must have at least one image_task.",
        "Every replacement task must include reference_component_ids/reference_unit_id so actual reference PNG can be passed to Image AI.",
        "Every task has max_retries=3.",
    ]
    payload.continuation_package.next_step_prompt = (
        "Continue with API calls #2A..#2N: execute only the prepared image_tasks. "
        "Each task is isolated; pass the full task contract and relevant continuation_package. "
        "Save every result as a PNG component and do not proceed to component QA until all planned tasks have a result."
    )

    updated = update_project_state(
        db,
        state.id,
        pipeline_stage="image_tasks",
        payload=payload,
        stage_result={"image_task_validation_issues": issues, "image_task_count": len(normalized)},
    )
    return ImageTaskPlan(
        project_state_id=updated.id,
        tasks=normalized,
        validation_issues=issues,
        ready=not any(_is_blocking_issue(i) for i in issues),
    )
