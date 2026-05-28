from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI
from PIL import Image
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.design_polish import DesignPolishResult
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class DesignPolishError(RuntimeError):
    pass


POLISH_DIR = Path("storage/renders")


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise DesignPolishError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _latest_render(payload) -> Dict[str, Any] | None:
    history = payload.render_history or []
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("render_path"):
                return item
    return None


def _latest_draft_qa(payload) -> Dict[str, Any] | None:
    status = payload.component_status or {}
    item = status.get("latest_draft_qa")
    return item if isinstance(item, dict) else None


def _decode_image_response(response: Any) -> bytes:
    data = response.data[0]
    b64_json = getattr(data, "b64_json", None)
    if not b64_json:
        raise DesignPolishError("OpenAI Images API не вернул b64_json.")
    return base64.b64decode(b64_json)


def _api_size_for_path(path: str) -> str:
    try:
        with Image.open(path) as img:
            w, h = img.size
        if w > h * 1.15:
            return "1536x1024"
        if h > w * 1.15:
            return "1024x1536"
    except Exception:
        pass
    return "1024x1024"


def _resize_like_source(image_bytes: bytes, source_path: str) -> bytes:
    try:
        with Image.open(source_path) as src:
            target_w, target_h = src.size
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        # Preserve full polished output without cropping; pad if aspect differs.
        img.thumbnail((target_w, target_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 255))
        canvas.alpha_composite(img, ((target_w - img.width) // 2, (target_h - img.height) // 2))
        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes


def _build_polish_prompt(payload, render: Dict[str, Any]) -> str:
    continuation = payload.continuation_package.model_dump()
    contract = continuation.get("strict_contract") or {}
    render_manifest = {
        "render_id": render.get("render_id"),
        "canvas": render.get("canvas"),
        "placed_blocks": render.get("placed_blocks"),
    }
    return f"""
Ты — Image AI design polish worker внутри production pipeline медицинской SMM-системы.

Тебе дан technical draft инфографики, уже собранный Python из утвержденных PNG-компонентов.
Твоя задача — ТОЛЬКО аккуратно улучшить визуальный стиль.

РАЗРЕШЕНО:
- улучшить фон, мягкие тени, глубину, цветовые акценты;
- сделать визуальную иерархию чище;
- слегка улучшить spacing и декоративные элементы;
- сделать макет более профессиональным и медицинским.

СТРОГО ЗАПРЕЩЕНО:
- менять текст внутри PNG-компонентов;
- переписывать, переводить или добавлять новые подписи;
- менять медицинские факты;
- менять состав semantic units;
- удалять блоки;
- менять порядок карточек;
- добавлять элементы, которых нет в contract;
- удалять обязательные компоненты;
- добавлять watermark, логотипы, UI соцсетей;
- превращать технический draft в новую инфографику с другой структурой.

Если сомневаешься — сохраняй technical draft почти без изменений.

STRICT CONTRACT:
{contract}

CONTINUATION PACKAGE:
{continuation}

RENDER MANIFEST:
{render_manifest}

Верни один polished PNG того же формата и приблизительно того же canvas. Сохрани структуру и все компоненты.
""".strip()


def _write_polished_bytes(state_id: int, render_id: Optional[str], image_bytes: bytes) -> str:
    out_dir = POLISH_DIR / f"state-{state_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    polish_id = f"polish-{uuid.uuid4().hex[:12]}"
    suffix = f"-{render_id}" if render_id else ""
    path = out_dir / f"{polish_id}{suffix}.png"
    path.write_bytes(image_bytes)
    return str(path)


def run_design_polish(db: Session, state_id: int) -> DesignPolishResult:
    """Stage 10: optional AI design polish for an approved technical draft.

    This stage never edits project semantics. It only creates a polished image
    candidate. Final QA decides later whether to use it or fall back to the
    technical draft.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    render = _latest_render(payload)
    if not render:
        raise DesignPolishError("No technical render found. Run /render/technical first.")
    render_path = str(render.get("render_path") or "")
    if not render_path or not Path(render_path).exists():
        raise DesignPolishError("Technical render file is missing.")

    latest_qa = _latest_draft_qa(payload)
    issues: List[str] = []
    if not latest_qa:
        raise DesignPolishError("Draft QA is missing. Run /draft/qa before design polish.")
    if not bool(latest_qa.get("draft_ok")):
        raise DesignPolishError("Draft QA is not successful. Run /draft/repair before design polish.")
    if latest_qa.get("recommendation") not in {"polish", "use_draft", None, ""}:
        raise DesignPolishError("Draft QA recommendation does not allow design polish. Run /draft/repair or use the draft.")

    prompt = _build_polish_prompt(payload, render)
    client = _get_client()
    try:
        image_file = open(render_path, "rb")
        try:
            response = client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
                size=_api_size_for_path(render_path),
                n=1,
            )
        finally:
            image_file.close()
        polished_bytes = _resize_like_source(_decode_image_response(response), render_path)
        polished_path = _write_polished_bytes(state_id, str(render.get("render_id") or ""), polished_bytes)
        status = "ready"
    except Exception as exc:
        polished_path = None
        status = "failed"
        issues.append(f"design_polish_failed:{type(exc).__name__}:{str(exc)[:240]}")

    record = {
        "status": status,
        "source_render_id": render.get("render_id"),
        "source_render_path": render_path,
        "polished_path": polished_path,
        "prompt": prompt,
        "issues": issues,
        "guardrails": {
            "do_not_change_text": True,
            "do_not_change_structure": True,
            "do_not_change_medical_facts": True,
            "do_not_delete_blocks": True,
        },
    }
    payload.component_status.setdefault("design_polish", []).append(record)
    payload.component_status["latest_design_polish"] = record
    payload.continuation_package.current_state_summary = (
        "Design polish candidate created." if status == "ready" else "Design polish failed; use technical draft or retry later."
    )
    payload.continuation_package.strict_contract.setdefault("design_polish", {})
    payload.continuation_package.strict_contract["design_polish"] = {
        "optional": True,
        "polish_may_be_rejected_by_final_qa": True,
        "technical_draft_is_fallback": True,
        "source_render_path": render_path,
        "polished_path": polished_path,
    }
    payload.continuation_package.next_step_prompt = (
        "Next step: run Final QA comparing polished image with the technical draft and the strict contract. "
        "If polish changed text, removed blocks, changed medical meaning, or distorted layout, use the technical draft."
    )

    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="design_polish",
        payload=payload,
        stage_result={"stage": "design_polish", "status": status, "polished_path": polished_path, "issues": issues},
    )
    return DesignPolishResult(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        status=status,  # type: ignore[arg-type]
        polished_path=polished_path,
        source_render_path=render_path,
        used_render_id=str(render.get("render_id") or "") or None,
        prompt=prompt,
        issues=issues,
        ready_for_final_qa=status == "ready",
        polish_record=record,
    )
