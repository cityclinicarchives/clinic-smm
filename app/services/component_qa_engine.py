from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI
from PIL import Image
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.component_qa import ComponentQAItem, ComponentQAResponse
from app.services.image_component_storage import load_component_manifest, save_component_manifest
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class ComponentQAError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ComponentQAError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _load_component_image_b64(path: str) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")


def _safe_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
    return {}


def _basic_image_checks(record: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    path = record.get("path")
    if not path or not Path(path).exists():
        return ["component_file_missing"]
    try:
        image = Image.open(path)
        w, h = image.size
        if w < 128 or h < 128:
            issues.append("component_too_small")
        # very rough blank check
        extrema = image.convert("RGB").getextrema()
        if all((mx - mn) < 8 for mn, mx in extrema):
            issues.append("component_almost_blank")
    except Exception as exc:
        issues.append(f"component_unreadable:{exc}")
    return issues


def _build_qa_prompt(record: Dict[str, Any], continuation: Dict[str, Any]) -> str:
    return f"""
Ты — аналитический QA-инспектор компонентов медицинской инфографики.

Проверь ОДИН PNG-компонент по его task contract. Твоя задача — решить, можно ли компонент использовать дальше.

КРИТИЧЕСКИЕ ПРАВИЛА:
- Проверяй только этот компонент, не финальную инфографику.
- Если task требовал убрать текст/watermark/UI/фон, а они видны — needs_repair.
- Если task требовал primary medical visual, а его нет — needs_repair.
- Если task требовал text_png_block, текст должен быть читаемым и не обрезанным.
- Если task требовал replacement в стиле reference_unit, оцени style consistency.
- OK-компоненты потом не перепроверяются, поэтому решение должно быть строгим.

CONTINUATION PACKAGE:
{json.dumps(continuation, ensure_ascii=False)[:6000]}

COMPONENT RECORD / TASK CONTRACT:
{json.dumps(record, ensure_ascii=False)[:10000]}

Верни строго JSON:
{{
  "ok": true,
  "score": 0.0,
  "problems": [],
  "repair_needed": false,
  "repair_instruction": "",
  "selected_as_best": false
}}
""".strip()


def _qa_component_with_ai(record: Dict[str, Any], continuation: Dict[str, Any]) -> ComponentQAItem:
    component_id = str(record.get("component_id") or "unknown")
    task_id = str(record.get("task_id") or "unknown")
    retry_count = int(record.get("retry_count") or 0)
    basic_issues = _basic_image_checks(record)
    if basic_issues:
        return ComponentQAItem(
            component_id=component_id,
            task_id=task_id,
            status="needs_repair",
            score=0.0,
            problems=basic_issues,
            repair_needed=True,
            repair_instruction="Regenerate the component because the PNG is missing, blank, unreadable, or too small.",
            retry_count=retry_count,
        )

    path = record.get("path")
    image_b64 = _load_component_image_b64(path)
    prompt = _build_qa_prompt(record, continuation)
    client = _get_client()
    try:
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                    ],
                }
            ],
        )
        data = _safe_json(response.output_text)
    except Exception as exc:
        return ComponentQAItem(
            component_id=component_id,
            task_id=task_id,
            status="failed",
            score=0.0,
            problems=[f"qa_model_error:{exc}"],
            repair_needed=False,
            retry_count=retry_count,
        )

    ok = bool(data.get("ok"))
    problems = data.get("problems") if isinstance(data.get("problems"), list) else []
    score = float(data.get("score") or (1.0 if ok else 0.0))
    repair_needed = bool(data.get("repair_needed") or not ok)
    return ComponentQAItem(
        component_id=component_id,
        task_id=task_id,
        status="ok" if ok else "needs_repair",
        score=score,
        problems=[str(p) for p in problems],
        repair_needed=repair_needed,
        repair_instruction=str(data.get("repair_instruction") or "") if repair_needed else None,
        retry_count=retry_count,
        selected_as_best=bool(data.get("selected_as_best")),
        metadata={"qa_model": settings.openai_model},
    )


def run_component_qa(db: Session, state_id: int, *, only_new_or_repaired: bool = True) -> ComponentQAResponse:
    """Stage 6: QA generated PNG components and create repair task intents.

    OK components are not rechecked by default. Components that need repair get
    a machine-readable repair instruction saved in ProjectState.component_status.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    status = payload.component_status or {}
    components = (status.get("components") or {}) if isinstance(status, dict) else {}
    if not components:
        raise ComponentQAError("No generated components found. Execute image tasks first.")

    qa_state = status.setdefault("component_qa", {})
    repair_tasks = status.setdefault("repair_tasks", {})
    retry_history = status.setdefault("retry_history", {})
    continuation = payload.continuation_package.model_dump()

    checked = ok_count = repair_count = failed_count = 0
    issues: List[str] = []

    for component_id, record in components.items():
        if not isinstance(record, dict):
            continue
        previous_qa = qa_state.get(component_id)
        if only_new_or_repaired and previous_qa and previous_qa.get("status") == "ok":
            continue
        if record.get("status") not in {"generated", "needs_repair"}:
            continue

        item = _qa_component_with_ai(record, continuation)
        checked += 1
        qa_state[component_id] = item.model_dump()
        if item.status == "ok":
            ok_count += 1
            record["status"] = "generated"
            record["best_version"] = True
            repair_tasks.pop(component_id, None)
        elif item.status == "needs_repair":
            repair_count += 1
            record["status"] = "needs_repair"
            repair_tasks[component_id] = {
                "component_id": component_id,
                "task_id": item.task_id,
                "repair_instruction": item.repair_instruction,
                "problems": item.problems,
                "retry_count": item.retry_count,
                "max_retries": int(record.get("metadata", {}).get("max_retries") or 3),
            }
            retry_history.setdefault(component_id, []).append({
                "event": "qa_needs_repair",
                "retry_count": item.retry_count,
                "score": item.score,
                "problems": item.problems,
            })
        else:
            failed_count += 1
            issues.extend(item.problems)
        components[component_id] = record

    payload.component_status = status
    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="component_qa",
        payload=payload,
        stage_result={
            "stage": "component_qa",
            "checked_count": checked,
            "ok_count": ok_count,
            "needs_repair_count": repair_count,
            "failed_count": failed_count,
            "repair_task_count": len(repair_tasks),
            "issues": issues,
        },
    )
    manifest = load_component_manifest(state_id)
    manifest["component_qa"] = qa_state
    manifest["repair_tasks"] = repair_tasks
    save_component_manifest(state_id, manifest)

    return ComponentQAResponse(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        checked_count=checked,
        ok_count=ok_count,
        needs_repair_count=repair_count,
        failed_count=failed_count,
        repair_task_count=len(repair_tasks),
        component_qa=qa_state,
        issues=issues,
    )
