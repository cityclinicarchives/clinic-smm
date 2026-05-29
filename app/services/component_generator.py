from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentAsset
from app.schemas.component_generation import ComponentGenerationResponse, ComponentRecord
from app.schemas.image_task import ImageTask
from app.schemas.project_state import ProjectStatePayload
from app.services.image_component_storage import save_component_bytes, upsert_component_record
from app.services.image_task_engine import prepare_image_tasks
from app.services.project_state_manager import get_payload, get_project_state, update_project_state
from app.services.telegram_bot import download_file_bytes


class ComponentGenerationError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ComponentGenerationError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _decode_image_response(response: Any) -> bytes:
    data = response.data[0]
    b64_json = getattr(data, "b64_json", None)
    if not b64_json:
        raise ComponentGenerationError("OpenAI Images API не вернул b64_json.")
    return base64.b64decode(b64_json)


def _task_component_id(task: ImageTask) -> str:
    if task.component_ids:
        return task.component_ids[0]
    return f"component_{task.task_id}"


def _task_contract(task: ImageTask) -> Dict[str, Any]:
    """Serialize a full machine-readable image-task contract for state storage."""
    try:
        return task.model_dump()
    except Exception:
        return {
            "task_id": task.task_id,
            "operation": task.operation,
            "final_unit_id": task.final_unit_id,
            "component_ids": task.component_ids,
            "reference_component_ids": task.reference_component_ids,
            "instruction_for_image_ai": task.instruction_for_image_ai,
            "must_include": task.must_include,
            "must_exclude": task.must_exclude,
            "qa_criteria": task.qa_criteria,
        }


def _source_unit_id(task: ImageTask) -> Optional[str]:
    value = task.metadata.get("source_unit_id") if isinstance(task.metadata, dict) else None
    return str(value) if value else None


def _reference_unit_id(task: ImageTask) -> Optional[str]:
    value = task.metadata.get("reference_unit_id") if isinstance(task.metadata, dict) else None
    return str(value) if value else None


def _source_bytes_for_state(db: Session, payload: ProjectStatePayload, asset_id: Optional[int]) -> Optional[bytes]:
    if not asset_id:
        return None
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if not asset or not asset.media_file_id or asset.media_type not in {"photo", "document"}:
        return None
    try:
        return download_file_bytes(asset.media_file_id)
    except Exception:
        return None


def _component_records(payload: ProjectStatePayload) -> Dict[str, Dict[str, Any]]:
    status = payload.component_status or {}
    components = status.get("components", {}) if isinstance(status, dict) else {}
    return components if isinstance(components, dict) else {}


def _find_reference_component_records(task: ImageTask, payload: ProjectStatePayload) -> List[Dict[str, Any]]:
    """Resolve reference_component_ids to generated component records.

    The analytical step may provide either component IDs or semantic/final-unit
    IDs as references. This resolver accepts both so replacement generation can
    use actual PNG references instead of relying only on text prompts.
    """
    components = _component_records(payload)
    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_record(cid: str, rec: Dict[str, Any]) -> None:
        path = rec.get("path")
        if not path or rec.get("status") not in {"generated", "approved", "qa_ok"}:
            return
        if cid in seen:
            return
        if not os.path.exists(str(path)):
            return
        refs.append(rec)
        seen.add(cid)

    for ref_id in task.reference_component_ids or []:
        ref = str(ref_id)
        direct = components.get(ref)
        if isinstance(direct, dict):
            add_record(ref, direct)
            continue
        for cid, rec in components.items():
            if not isinstance(rec, dict):
                continue
            if str(rec.get("final_unit_id") or "") == ref or str(rec.get("source_unit_id") or "") == ref:
                add_record(str(cid), rec)
    return refs


def _reference_summary(task: ImageTask, payload: ProjectStatePayload) -> Dict[str, Any]:
    records = _find_reference_component_records(task, payload)
    return {
        "reference_component_ids": task.reference_component_ids,
        "resolved_reference_count": len(records),
        "resolved_references": [
            {
                "component_id": r.get("component_id"),
                "final_unit_id": r.get("final_unit_id"),
                "operation": r.get("operation"),
                "path": r.get("path"),
                "output_size": r.get("output_size"),
            }
            for r in records
        ],
    }


def _open_named_png(path: str, name: str) -> io.BytesIO:
    with open(path, "rb") as f:
        data = f.read()
    bio = io.BytesIO(data)
    bio.name = name
    return bio


def _build_task_prompt(task: ImageTask, payload: ProjectStatePayload, project_state_id: int | None = None) -> str:
    continuation = payload.continuation_package
    contract = continuation.strict_contract or {}
    current_state = {
        "project_state_id": project_state_id,
        "stage": getattr(payload, "pipeline_stage", ""),
        "final_units_count": len(payload.final_units or []),
        "image_tasks_count": len(payload.image_tasks or []),
    }
    reference_summary = _reference_summary(task, payload)

    return f"""
Ты — Image AI worker внутри production pipeline медицинской SMM-системы.

Ты выполняешь ОДНУ атомарную задачу и должен вернуть ОДИН готовый PNG-компонент.
Не создавай финальную инфографику целиком.
Не добавляй лишний текст, если операция не является generate_text_png_block.
Не игнорируй must_include/must_exclude: это контракт, а не пожелания.

CURRENT STATE:
{current_state}

STRICT CONTRACT:
{contract}

IMAGE TASK CONTRACT:
- task_id: {task.task_id}
- operation: {task.operation}
- final_unit_id: {task.final_unit_id}
- component_ids: {task.component_ids}
- reference_component_ids: {task.reference_component_ids}
- source_image_required: {task.source_image_required}
- transparent_or_neutral_background: {task.transparent_or_neutral_background}

RESOLVED REFERENCE COMPONENTS:
{reference_summary}

ИНСТРУКЦИЯ:
{task.instruction_for_image_ai}

ОБЯЗАТЕЛЬНО ВКЛЮЧИТЬ:
{task.must_include}

ОБЯЗАТЕЛЬНО ИСКЛЮЧИТЬ:
{task.must_exclude}

QA КРИТЕРИИ БУДУЩЕЙ ПРОВЕРКИ:
{task.qa_criteria}

ТРЕБОВАНИЯ К РЕЗУЛЬТАТУ:
- output = самостоятельный reusable PNG component;
- не добавляй watermark, UI, старые подписи или мусор из исходника;
- не искажай кириллицу;
- не меняй медицинский смысл;
- если это text_png_block, текст должен быть крупным, читаемым, без обрезки;
- если это replacement_unit, наследуй стиль не только из текста, но и из приложенных reference PNG;
- если reference PNG приложен, используй его как главный визуальный эталон масштаба, палитры, освещения, толщины линий, композиции и уровня детализации;
- не копируй содержание reference PNG буквально, копируй только стиль и визуальную грамматику;
- строго соблюдай формат компонента.
""".strip()


def _api_size_for(task: ImageTask) -> str:
    """Use conservative sizes accepted by image models, then resize locally.

    Image task contracts can request arbitrary component sizes (for example
    512x512 or 420x240). Image APIs usually accept a small fixed set of output
    sizes, so the program requests a compatible canvas and post-processes to the
    exact task size.
    """
    w = task.output_png_size.w
    h = task.output_png_size.h
    if w > h * 1.15:
        return "1536x1024"
    if h > w * 1.15:
        return "1024x1536"
    return "1024x1024"


def _resize_to_task_size(image_bytes: bytes, task: ImageTask) -> bytes:
    target_w = task.output_png_size.w
    target_h = task.output_png_size.h
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        # Fit inside requested size without cropping; pad with transparency.
        image.thumbnail((target_w, target_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
        x = (target_w - image.width) // 2
        y = (target_h - image.height) // 2
        canvas.alpha_composite(image, (x, y))
        out = io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes


def _generate_component_with_images_api(
    *,
    client: OpenAI,
    task: ImageTask,
    prompt: str,
    source_bytes: Optional[bytes],
    reference_records: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    request_size = _api_size_for(task)
    reference_records = reference_records or []

    # Use image edit whenever there are visual inputs. For replacement units,
    # actual reference PNGs are passed as images so the image model can inherit
    # style from real components instead of only reading a text description.
    image_inputs: List[io.BytesIO] = []
    if task.operation == "extract_component" and source_bytes:
        src = io.BytesIO(source_bytes)
        src.name = "source.png"
        image_inputs.append(src)
    if task.operation == "generate_replacement_unit":
        for idx, record in enumerate(reference_records[:3]):
            path = record.get("path")
            if path and os.path.exists(str(path)):
                image_inputs.append(_open_named_png(str(path), f"reference_{idx+1}.png"))

    if image_inputs:
        try:
            image_arg: Any = image_inputs[0] if len(image_inputs) == 1 else image_inputs
            response = client.images.edit(
                model=settings.openai_image_model,
                image=image_arg,
                prompt=prompt,
                size=request_size,
                n=1,
            )
            return _resize_to_task_size(_decode_image_response(response), task)
        except Exception:
            # Fallback to generation with the same strict task contract if edit
            # is not available for the selected model/runtime. The prompt still
            # contains a reference summary, but quality may be lower.
            pass

    response = client.images.generate(
        model=settings.openai_image_model,
        prompt=prompt,
        size=request_size,
        n=1,
    )
    return _resize_to_task_size(_decode_image_response(response), task)


def _existing_status(payload: ProjectStatePayload) -> Dict[str, Any]:
    status = payload.component_status or {}
    if not isinstance(status, dict):
        status = {}
    status.setdefault("components", {})
    status.setdefault("failed", {})
    status.setdefault("generated_order", [])
    status.setdefault("task_to_component", {})
    status.setdefault("manifest_version", 2)
    return status


def _build_record(
    *,
    task: ImageTask,
    component_id: str,
    status: str,
    prompt: Optional[str] = None,
    path: Optional[str] = None,
    error: Optional[str] = None,
    retry_count: int = 0,
) -> ComponentRecord:
    return ComponentRecord(
        component_id=component_id,
        task_id=task.task_id,
        final_unit_id=task.final_unit_id,
        operation=task.operation,
        source_unit_id=_source_unit_id(task),
        reference_unit_id=_reference_unit_id(task),
        reference_component_ids=task.reference_component_ids,
        path=path,
        status=status,  # type: ignore[arg-type]
        retry_count=retry_count,
        best_version=status == "generated",
        error=error,
        prompt=prompt,
        instruction_for_image_ai=task.instruction_for_image_ai,
        must_include=task.must_include,
        must_exclude=task.must_exclude,
        qa_criteria=task.qa_criteria,
        source_image_required=task.source_image_required,
        output_size={"w": task.output_png_size.w, "h": task.output_png_size.h},
        task_contract=_task_contract(task),
        metadata={
            **(task.metadata or {}),
            "transparent_or_neutral_background": task.transparent_or_neutral_background,
            "max_retries": task.max_retries,
        },
    )


def execute_image_tasks(db: Session, state_id: int, *, only_failed: bool = False, prepare: bool = True) -> ComponentGenerationResponse:
    """Stage 5: execute atomic image tasks and persist PNG components.

    Every task produces exactly one reusable PNG component. The full task
    contract is saved with the component so later QA/repair/layout stages do not
    depend on model memory.
    """
    if prepare:
        plan = prepare_image_tasks(db, state_id)
    else:
        state_for_plan = get_project_state(db, state_id)
        payload_for_plan = get_payload(state_for_plan)
        plan = type("AdHocImageTaskPlan", (), {"tasks": [ImageTask.model_validate(t) for t in (payload_for_plan.image_tasks or [])]})()
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    source_bytes = _source_bytes_for_state(db, payload, state.asset_id)
    client = _get_client()

    component_status = _existing_status(payload)
    generated_count = 0
    skipped_count = 0
    failed_count = 0
    issues: List[str] = []

    for task in plan.tasks:
        component_id = _task_component_id(task)
        existing = component_status.get("components", {}).get(component_id)
        if existing and existing.get("status") == "generated" and not only_failed:
            skipped_count += 1
            continue
        if only_failed and existing and existing.get("status") != "failed":
            skipped_count += 1
            continue

        prompt = _build_task_prompt(task, payload, project_state_id=state_id)
        reference_records = _find_reference_component_records(task, payload)
        previous_retry = int((existing or {}).get("retry_count", 0)) if isinstance(existing, dict) else 0
        try:
            image_bytes = _generate_component_with_images_api(
                client=client,
                task=task,
                prompt=prompt,
                source_bytes=source_bytes,
                reference_records=reference_records,
            )
            path = save_component_bytes(state_id, task.task_id, image_bytes, component_id)
            record = _build_record(
                task=task,
                component_id=component_id,
                status="generated",
                prompt=prompt,
                path=path,
                retry_count=previous_retry,
            )
            component_status["components"][component_id] = record.model_dump()
            component_status["task_to_component"][task.task_id] = component_id
            if component_id not in component_status["generated_order"]:
                component_status["generated_order"].append(component_id)
            component_status.get("failed", {}).pop(component_id, None)
            upsert_component_record(state_id, record)
            generated_count += 1
        except Exception as exc:
            failed_count += 1
            issues.append(f"task_failed:{task.task_id}:{exc}")
            record = _build_record(
                task=task,
                component_id=component_id,
                status="failed",
                error=str(exc),
                retry_count=previous_retry + 1,
            )
            component_status["components"][component_id] = record.model_dump()
            component_status["failed"][component_id] = str(exc)
            component_status["task_to_component"][task.task_id] = component_id
            upsert_component_record(state_id, record)

    payload.component_status = component_status
    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="image_tasks",
        payload=payload,
        stage_result={
            "stage": "component_generation",
            "generated_count": generated_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "issues": issues,
        },
    )

    return ComponentGenerationResponse(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        generated_count=generated_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        component_status=component_status,
        issues=issues,
    )
