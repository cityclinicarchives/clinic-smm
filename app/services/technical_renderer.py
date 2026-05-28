from __future__ import annotations

from pathlib import Path
import uuid
from typing import Any, Dict, List, Tuple

from PIL import Image
from sqlalchemy.orm import Session

from app.schemas.final_layout import FinalLayoutBlueprint
from app.schemas.technical_render import TechnicalRenderManifest, TechnicalRenderResult
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class TechnicalRenderError(RuntimeError):
    pass


RENDERS_DIR = Path("storage/renders")


def _safe_color(value: str | None) -> tuple[int, int, int, int]:
    raw = (value or "#FFFFFF").strip()
    if raw.startswith("#") and len(raw) in {7, 9}:
        try:
            r = int(raw[1:3], 16)
            g = int(raw[3:5], 16)
            b = int(raw[5:7], 16)
            a = int(raw[7:9], 16) if len(raw) == 9 else 255
            return (r, g, b, a)
        except Exception:
            pass
    return (255, 255, 255, 255)


def _component_records(payload) -> dict[str, dict[str, Any]]:
    status = payload.component_status or {}
    records = status.get("components") or {}
    return records if isinstance(records, dict) else {}


def _resolve_component_path(component_id: str, records: dict[str, dict[str, Any]]) -> str | None:
    record = records.get(component_id)
    if not isinstance(record, dict):
        return None
    path = record.get("path") or record.get("best_path")
    return str(path) if path else None


def _resize_image(img: Image.Image, target_w: int, target_h: int, fit_mode: str = "contain") -> Image.Image:
    if target_w <= 0 or target_h <= 0:
        raise TechnicalRenderError("Invalid target block size")

    source = img.convert("RGBA")
    sw, sh = source.size
    if sw <= 0 or sh <= 0:
        raise TechnicalRenderError("Invalid component image size")

    if fit_mode == "cover":
        scale = max(target_w / sw, target_h / sh)
        resized = source.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)
        rw, rh = resized.size
        left = max(0, (rw - target_w) // 2)
        top = max(0, (rh - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    scale = min(target_w / sw, target_h / sh)
    resized = source.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)
    layer = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
    x = (target_w - resized.size[0]) // 2
    y = (target_h - resized.size[1]) // 2
    layer.alpha_composite(resized, (x, y))
    return layer


def _validate_layout_before_render(blueprint: FinalLayoutBlueprint, records: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    canvas = blueprint.canvas
    if canvas.w <= 0 or canvas.h <= 0:
        issues.append("invalid_canvas_size")
    if not blueprint.blocks:
        issues.append("no_layout_blocks")

    seen: set[str] = set()
    for block in blueprint.blocks:
        if not block.component_id:
            issues.append(f"block_without_component:{block.block_id}")
            continue
        if block.component_id in seen:
            issues.append(f"component_placed_more_than_once:{block.component_id}")
        seen.add(block.component_id)
        path = _resolve_component_path(block.component_id, records)
        if not path:
            issues.append(f"component_path_missing:{block.component_id}")
        elif not Path(path).exists():
            issues.append(f"component_file_missing:{block.component_id}")
        if block.w <= 0 or block.h <= 0:
            issues.append(f"block_invalid_size:{block.block_id}")
        if block.x < 0 or block.y < 0 or block.x + block.w > canvas.w or block.y + block.h > canvas.h:
            issues.append(f"block_outside_canvas:{block.block_id}")
    return issues


def render_technical_draft(db: Session, state_id: int) -> TechnicalRenderResult:
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    if not payload.layout_blueprint:
        raise TechnicalRenderError("No final_layout_blueprint found. Run /layout/finalize first.")

    try:
        blueprint = FinalLayoutBlueprint.model_validate(payload.layout_blueprint)
    except Exception as exc:
        raise TechnicalRenderError(f"Invalid final_layout_blueprint: {exc}") from exc

    records = _component_records(payload)
    issues = _validate_layout_before_render(blueprint, records)
    if issues:
        # Do not render a misleading draft if the blueprint cannot be executed.
        payload.component_status.setdefault("technical_render", {})
        payload.component_status["technical_render"] = TechnicalRenderManifest(
            status="failed",
            canvas=blueprint.canvas.model_dump(),
            issues=issues,
        ).model_dump()
        new_state = update_project_state(
            db,
            state_id,
            pipeline_stage="technical_render",
            payload=payload,
            stage_result={"stage": "technical_render", "ready": False, "issues": issues},
        )
        return TechnicalRenderResult(
            project_state_id=state_id,
            pipeline_stage=new_state.pipeline_stage,
            state_version=new_state.state_version,
            canvas=blueprint.canvas.model_dump(),
            placed_block_count=0,
            expected_block_count=len(blueprint.blocks),
            issues=issues,
            ready=False,
        )

    canvas = Image.new("RGBA", (blueprint.canvas.w, blueprint.canvas.h), _safe_color(blueprint.canvas.background))
    placed: list[dict[str, Any]] = []
    for block in sorted(blueprint.blocks, key=lambda b: b.z_index):
        path = _resolve_component_path(block.component_id, records)
        if not path:
            continue
        try:
            with Image.open(path) as img:
                layer = _resize_image(img, block.w, block.h, block.fit_mode)
            canvas.alpha_composite(layer, (block.x, block.y))
            placed.append({
                "block_id": block.block_id,
                "component_id": block.component_id,
                "x": block.x,
                "y": block.y,
                "w": block.w,
                "h": block.h,
                "z_index": block.z_index,
                "fit_mode": block.fit_mode,
            })
        except Exception as exc:
            issues.append(f"render_failed:{block.block_id}:{type(exc).__name__}:{str(exc)[:160]}")

    out_dir = RENDERS_DIR / f"state-{state_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    render_id = f"render-{uuid.uuid4().hex[:12]}"
    out_path = out_dir / f"{render_id}.png"
    canvas.convert("RGB").save(out_path, "PNG")

    ready = not issues and len(placed) == len(blueprint.blocks)
    manifest = TechnicalRenderManifest(
        render_id=render_id,
        status="ready" if ready else "failed",
        render_path=str(out_path),
        canvas=blueprint.canvas.model_dump(),
        placed_blocks=placed,
        issues=issues,
    )
    payload.component_status.setdefault("technical_render", {})
    payload.component_status["technical_render"] = manifest.model_dump()
    payload.render_history.append(manifest.model_dump())
    payload.continuation_package.current_state_summary = (
        f"Technical draft rendered from {len(placed)} PNG components on canvas "
        f"{blueprint.canvas.w}x{blueprint.canvas.h}."
    )
    payload.continuation_package.strict_contract.setdefault("technical_render", {})
    payload.continuation_package.strict_contract["technical_render"] = {
        "python_only_places_png_components": True,
        "python_does_not_draw_text": True,
        "expected_block_count": len(blueprint.blocks),
        "placed_block_count": len(placed),
        "render_path": str(out_path),
    }
    payload.continuation_package.next_step_prompt = (
        "Next step: run Draft QA on the technical draft. Check that all PNG blocks are present, "
        "nothing is cropped, layout is visually balanced, and no component is missing."
    )

    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="technical_render",
        payload=payload,
        stage_result={
            "stage": "technical_render",
            "ready": ready,
            "render_path": str(out_path),
            "placed_block_count": len(placed),
            "expected_block_count": len(blueprint.blocks),
            "issues": issues,
        },
    )

    return TechnicalRenderResult(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        render_path=str(out_path),
        canvas=blueprint.canvas.model_dump(),
        placed_block_count=len(placed),
        expected_block_count=len(blueprint.blocks),
        issues=issues,
        ready=ready,
    )
