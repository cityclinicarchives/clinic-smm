from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from openai import OpenAI
from PIL import Image
from sqlalchemy.orm import Session

from app.config import settings

from app.schemas.final_layout import CanvasSpec, FinalLayoutBlueprint, FinalLayoutResponse, LayoutBlock, LayoutComponent
from app.services.image_component_storage import load_component_manifest
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class FinalLayoutError(RuntimeError):
    pass


ASPECTS: list[tuple[str, int, int]] = [
    ("1:1", 1080, 1080),
    ("4:5", 1080, 1350),
    ("3:4", 1080, 1440),
    ("2:3", 1080, 1620),
    ("9:16", 1080, 1920),
]


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise FinalLayoutError("AI layout refinement must return a JSON object")
    return data


def _get_client() -> OpenAI | None:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        return None
    return OpenAI(api_key=settings.openai_api_key)


def _component_brief(components: list[LayoutComponent]) -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    for comp in components:
        brief.append({
            "component_id": comp.component_id,
            "final_unit_id": comp.final_unit_id,
            "component_type": comp.component_type,
            "operation": comp.operation,
            "w": comp.w,
            "h": comp.h,
            "metadata": comp.metadata,
        })
    return brief


def _build_ai_layout_prompt(payload, components: list[LayoutComponent], fallback: FinalLayoutBlueprint) -> str:
    return (
        "You are the final layout refinement engine for a medical SMM infographic.\n"
        "You do NOT create new content and do NOT create text. All components are already finished PNG files.\n"
        "Your task is only to produce a machine-readable layout blueprint that places every approved PNG component on a canvas.\n\n"
        "Rules:\n"
        "1. Place EVERY component exactly once unless component_type='background'.\n"
        "2. Do not invent new component_id values.\n"
        "3. Do not remove approved components.\n"
        "4. Prefer 4:5 for standard infographics; escalate to 3:4, 2:3 or 9:16 if needed.\n"
        "5. Avoid overlaps unless a component_type='background'.\n"
        "6. Nothing may be outside the canvas.\n"
        "7. Text is already inside text_png_block components; do not ask Python to draw text.\n"
        "8. Output JSON only.\n\n"
        "Return schema:\n"
        "{\n"
        "  \"canvas\": {\"aspect_ratio\": \"4:5|3:4|2:3|9:16|1:1\", \"w\": 1080, \"h\": 1350, \"background\": \"#FFFFFF\"},\n"
        "  \"blocks\": [\n"
        "    {\"block_id\": \"block_component_id\", \"component_id\": \"existing_component_id\", \"x\": 0, \"y\": 0, \"w\": 100, \"h\": 100, \"z_index\": 1, \"fit_mode\": \"contain\"}\n"
        "  ],\n"
        "  \"layout_notes\": []\n"
        "}\n\n"
        f"Continuation package:\n{json.dumps(payload.continuation_package.model_dump(), ensure_ascii=False)[:12000]}\n\n"
        f"Original layout_blueprint from master call:\n{json.dumps(payload.layout_blueprint or {}, ensure_ascii=False)[:12000]}\n\n"
        f"Approved PNG components:\n{json.dumps(_component_brief(components), ensure_ascii=False)[:16000]}\n\n"
        f"Safe deterministic fallback blueprint to improve if useful:\n{json.dumps(fallback.model_dump(), ensure_ascii=False)[:12000]}"
    )


def _ai_refine_layout(payload, components: list[LayoutComponent], fallback: FinalLayoutBlueprint) -> tuple[FinalLayoutBlueprint | None, list[str]]:
    issues: list[str] = []
    client = _get_client()
    if client is None:
        return None, ["ai_layout_refinement_skipped:no_openai_key"]
    try:
        prompt = _build_ai_layout_prompt(payload, components, fallback)
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": "You are a strict JSON layout planner. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        data = _extract_json_object(response.output_text)
        blueprint = FinalLayoutBlueprint.model_validate(data)
        validation = _validate_layout(blueprint, components)
        blueprint.validation_issues = validation
        if validation:
            blueprint.status = "failed"
            return None, [f"ai_layout_invalid:{issue}" for issue in validation[:12]]
        blueprint.status = "ready"
        blueprint.layout_notes.append("Stage 7 AI-refined layout built from approved PNG components.")
        return blueprint, []
    except Exception as exc:
        return None, [f"ai_layout_refinement_failed:{type(exc).__name__}:{str(exc)[:250]}"]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _image_size(path: str) -> tuple[int, int]:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


def _component_type(record: Dict[str, Any]) -> str:
    op = str(record.get("operation") or "")
    if op == "generate_text_png_block":
        return "text_png_block"
    if op == "generate_replacement_unit":
        return "replacement_visual"
    if op == "extract_component":
        return "visual_component"
    if op == "generate_icon":
        return "icon"
    if op == "generate_background":
        return "background"
    return op or "component"


def _load_approved_components(state_id: int, payload) -> tuple[list[LayoutComponent], list[str]]:
    issues: list[str] = []
    status = payload.component_status or {}
    components = status.get("components") or {}
    qa = status.get("component_qa") or {}
    manifest = load_component_manifest(state_id)
    manifest_components = (manifest.get("components") or {}) if isinstance(manifest, dict) else {}

    approved: list[LayoutComponent] = []
    for component_id, record in components.items():
        if not isinstance(record, dict):
            continue
        # Prefer manifest record if it has fresher path metadata.
        if isinstance(manifest_components.get(component_id), dict):
            record = {**record, **manifest_components[component_id]}
        record_status = str(record.get("status") or "")
        qa_record = qa.get(component_id) if isinstance(qa, dict) else None
        qa_status = str((qa_record or {}).get("status") or "") if isinstance(qa_record, dict) else ""
        is_approved = record_status == "generated" and (not qa_status or qa_status == "ok")
        # If QA has not run yet, generated components are allowed but the layout
        # stage records a warning. This keeps the endpoint useful during testing.
        if record_status == "generated" and not qa_status:
            issues.append(f"component_without_qa:{component_id}")
            is_approved = True
        if not is_approved:
            continue
        path = str(record.get("path") or "")
        if not path or not Path(path).exists():
            issues.append(f"approved_component_file_missing:{component_id}")
            continue
        w, h = _image_size(path)
        if w <= 0 or h <= 0:
            issues.append(f"approved_component_unreadable:{component_id}")
            continue
        approved.append(
            LayoutComponent(
                component_id=str(component_id),
                path=path,
                w=w,
                h=h,
                final_unit_id=str(record.get("final_unit_id") or "") or None,
                component_type=_component_type(record),
                operation=str(record.get("operation") or "") or None,
                status=record_status,
                qa_status=qa_status or None,
                metadata={
                    "task_id": record.get("task_id"),
                    "source_unit_id": record.get("source_unit_id"),
                    "reference_unit_id": record.get("reference_unit_id"),
                    "must_include": record.get("must_include") or [],
                    "must_exclude": record.get("must_exclude") or [],
                    "output_size": record.get("output_size") or {},
                },
            )
        )
    return approved, issues


def _rank_components(components: list[LayoutComponent]) -> list[LayoutComponent]:
    priority = {
        "background": 0,
        "text_png_block": 1,
        "visual_component": 2,
        "replacement_visual": 2,
        "icon": 3,
    }
    # Keep the source ordering as much as possible, but put header/text blocks first.
    return sorted(
        components,
        key=lambda c: (
            priority.get(c.component_type or "", 5),
            c.final_unit_id or c.component_id,
            c.component_id,
        ),
    )


def _choose_canvas(component_count: int, requested: Dict[str, Any] | None = None) -> CanvasSpec:
    if requested:
        canvas = requested.get("canvas") or requested
        w = _safe_int(canvas.get("w") or canvas.get("width"), 0) if isinstance(canvas, dict) else 0
        h = _safe_int(canvas.get("h") or canvas.get("height"), 0) if isinstance(canvas, dict) else 0
        if w >= 800 and h >= 800:
            ratio = str(canvas.get("aspect_ratio") or f"{w}:{h}") if isinstance(canvas, dict) else f"{w}:{h}"
            return CanvasSpec(aspect_ratio=ratio, w=w, h=h)
    # Universal default: 4:5 for most SMM infographics, escalated for complex ones.
    if component_count <= 6:
        return CanvasSpec(aspect_ratio="4:5", w=1080, h=1350)
    if component_count <= 12:
        return CanvasSpec(aspect_ratio="3:4", w=1080, h=1440)
    if component_count <= 18:
        return CanvasSpec(aspect_ratio="2:3", w=1080, h=1620)
    return CanvasSpec(aspect_ratio="9:16", w=1080, h=1920)


def _grid_dimensions(n: int) -> tuple[int, int]:
    if n <= 1:
        return (1, 1)
    if n <= 4:
        return (2, math.ceil(n / 2))
    if n <= 9:
        return (3, math.ceil(n / 3))
    if n <= 16:
        return (4, math.ceil(n / 4))
    return (4, math.ceil(n / 4))


def _build_fallback_layout(components: list[LayoutComponent], canvas: CanvasSpec) -> list[LayoutBlock]:
    if not components:
        return []
    margin = 48
    gap = 24
    ordered = _rank_components(components)

    # Background components occupy full canvas, if present.
    blocks: list[LayoutBlock] = []
    foreground = []
    z = 0
    for comp in ordered:
        if comp.component_type == "background":
            blocks.append(LayoutBlock(
                block_id=f"block_{comp.component_id}",
                component_id=comp.component_id,
                x=0,
                y=0,
                w=canvas.w,
                h=canvas.h,
                z_index=z,
                fit_mode="cover",
            ))
            z += 1
        else:
            foreground.append(comp)

    if not foreground:
        return blocks

    # If there are obvious wide text/header blocks, give them full width.
    header_like: list[LayoutComponent] = []
    cards: list[LayoutComponent] = []
    footer_like: list[LayoutComponent] = []
    for comp in foreground:
        name = f"{comp.component_id} {comp.final_unit_id or ''}".lower()
        if comp.component_type == "text_png_block" and any(k in name for k in ["title", "header", "headline"]):
            header_like.append(comp)
        elif comp.component_type == "text_png_block" and any(k in name for k in ["footer", "warning", "cta", "disclaimer", "note", "action"]):
            footer_like.append(comp)
        else:
            cards.append(comp)

    y = margin
    full_w = canvas.w - margin * 2
    for comp in header_like:
        h = min(max(120, int(full_w * comp.h / max(comp.w, 1))), 220)
        blocks.append(LayoutBlock(
            block_id=f"block_{comp.component_id}",
            component_id=comp.component_id,
            x=margin,
            y=y,
            w=full_w,
            h=h,
            z_index=z,
        ))
        z += 1
        y += h + gap

    footer_reserved = 0
    if footer_like:
        footer_reserved = min(360, max(120, 140 * len(footer_like))) + gap
    available_h = max(360, canvas.h - y - margin - footer_reserved)

    cols, rows = _grid_dimensions(len(cards))
    cell_w = int((canvas.w - margin * 2 - gap * (cols - 1)) / cols)
    cell_h = int((available_h - gap * max(rows - 1, 0)) / max(rows, 1))
    min_cell_h = 160
    if cell_h < min_cell_h:
        # Escalate virtual layout height. Canvas escalation is stored in the
        # blueprint and later stages can render this larger canvas safely.
        cell_h = min_cell_h
    for idx, comp in enumerate(cards):
        row = idx // cols
        col = idx % cols
        x = margin + col * (cell_w + gap)
        cy = y + row * (cell_h + gap)
        blocks.append(LayoutBlock(
            block_id=f"block_{comp.component_id}",
            component_id=comp.component_id,
            x=x,
            y=cy,
            w=cell_w,
            h=cell_h,
            z_index=z,
            fit_mode="contain",
            metadata={"layout_group": "cards", "row": row, "col": col},
        ))
        z += 1
    if cards:
        y += rows * cell_h + max(rows - 1, 0) * gap + gap

    for comp in footer_like:
        h = min(max(110, int(full_w * comp.h / max(comp.w, 1))), 180)
        if y + h + margin > canvas.h:
            # Let the canvas grow instead of clipping.
            canvas.h = y + h + margin
        blocks.append(LayoutBlock(
            block_id=f"block_{comp.component_id}",
            component_id=comp.component_id,
            x=margin,
            y=y,
            w=full_w,
            h=h,
            z_index=z,
            metadata={"layout_group": "footer"},
        ))
        z += 1
        y += h + gap

    if y + margin > canvas.h:
        canvas.h = y + margin
    return blocks


def _validate_layout(blueprint: FinalLayoutBlueprint, components: list[LayoutComponent]) -> list[str]:
    issues: list[str] = []
    ids = {c.component_id for c in components}
    used = set()
    for block in blueprint.blocks:
        if block.component_id not in ids:
            issues.append(f"layout_block_unknown_component:{block.block_id}:{block.component_id}")
        used.add(block.component_id)
        if block.w <= 0 or block.h <= 0:
            issues.append(f"layout_block_invalid_size:{block.block_id}")
        if block.x < 0 or block.y < 0:
            issues.append(f"layout_block_negative_position:{block.block_id}")
        if block.x + block.w > blueprint.canvas.w:
            issues.append(f"layout_block_overflows_width:{block.block_id}")
        if block.y + block.h > blueprint.canvas.h:
            issues.append(f"layout_block_overflows_height:{block.block_id}")
    for component_id in ids - used:
        issues.append(f"component_not_placed:{component_id}")
    return issues


def finalize_layout(db: Session, state_id: int) -> FinalLayoutResponse:
    """Stage 7: build a final layout blueprint from approved PNG components.

    This stage does not render the image. It creates a machine-readable layout
    that Python can execute in Stage 8. The function intentionally uses a
    deterministic fallback layout so the pipeline remains stable even if an AI
    layout-refinement call is unavailable.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)

    components, issues = _load_approved_components(state_id, payload)
    if not components:
        raise FinalLayoutError("No approved PNG components found. Run image tasks and component QA first.")

    canvas = _choose_canvas(len(components), payload.layout_blueprint)
    fallback_blocks = _build_fallback_layout(components, canvas)
    fallback_blueprint = FinalLayoutBlueprint(
        canvas=canvas,
        blocks=fallback_blocks,
        layout_notes=[
            "Stage 7 deterministic fallback layout built from approved PNG components.",
            "Python will render only PNG components at these coordinates in Stage 8.",
            "Text is expected to already be inside text_png_block components.",
        ],
        status="ready",
    )
    fallback_validation = _validate_layout(fallback_blueprint, components)
    fallback_blueprint.validation_issues = fallback_validation
    if fallback_validation:
        fallback_blueprint.status = "failed"

    ai_blueprint, ai_issues = _ai_refine_layout(payload, components, fallback_blueprint)
    if ai_blueprint is not None:
        blueprint = ai_blueprint
        validation = []
    else:
        blueprint = fallback_blueprint
        validation = fallback_validation
        issues.extend(ai_issues)

    blueprint.validation_issues = issues + validation
    if validation:
        # Critical placement issues make the layout not ready. Warnings about
        # missing QA are allowed but visible.
        blueprint.status = "failed"
    else:
        blueprint.status = "ready"

    payload.layout_blueprint = blueprint.model_dump()
    layout_version = {
        "stage": "final_layout_refinement",
        "canvas": blueprint.canvas.model_dump(),
        "block_count": len(blueprint.blocks),
        "component_count": len(components),
        "validation_issues": list(blueprint.validation_issues),
        "status": blueprint.status,
    }
    payload.layout_versions.append(layout_version)
    payload.approved_component_versions = {
        component.component_id: {
            "path": component.path,
            "w": component.w,
            "h": component.h,
            "component_type": component.component_type,
            "qa_status": component.qa_status,
            "final_unit_id": component.final_unit_id,
        }
        for component in components
    }
    # Add a stage-specific contract fragment to continuation package so later
    # calls can restore the chain after this API break.
    payload.continuation_package.current_state_summary = (
        f"Final layout blueprint prepared with {len(components)} approved PNG components and {len(blueprint.blocks)} placed blocks."
    )
    payload.continuation_package.strict_contract.setdefault("final_layout", {})
    payload.continuation_package.strict_contract["final_layout"] = {
        "python_renders_only_png_components": True,
        "do_not_redraw_text_in_python": True,
        "component_count": len(components),
        "block_count": len(blueprint.blocks),
        "canvas": blueprint.canvas.model_dump(),
    }
    payload.continuation_package.next_step_prompt = (
        "Next step: render a technical draft by placing approved PNG components exactly according to final_layout_blueprint. "
        "Do not create new text or redesign components during technical render."
    )

    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="layout_refinement",
        payload=payload,
        stage_result={
            "stage": "final_layout_refinement",
            "approved_component_count": len(components),
            "block_count": len(blueprint.blocks),
            "validation_issues": blueprint.validation_issues,
            "ready": blueprint.status == "ready",
        },
    )

    return FinalLayoutResponse(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        approved_component_count=len(components),
        block_count=len(blueprint.blocks),
        layout_blueprint=blueprint.model_dump(),
        validation_issues=blueprint.validation_issues,
        ready=blueprint.status == "ready",
    )
