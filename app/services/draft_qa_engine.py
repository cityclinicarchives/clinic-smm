from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI
from PIL import Image
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.draft_qa import DraftQAResult
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class DraftQAError(RuntimeError):
    pass


def _get_client() -> OpenAI | None:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        return None
    return OpenAI(api_key=settings.openai_api_key)


def _safe_json(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _image_b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _latest_render(payload) -> Dict[str, Any] | None:
    history = payload.render_history or []
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("render_path"):
                return item
    return None


def _basic_render_checks(render: Dict[str, Any], payload) -> list[str]:
    issues: list[str] = []
    path = str(render.get("render_path") or "")
    if not path:
        return ["render_path_missing"]
    if not Path(path).exists():
        return ["render_file_missing"]
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w < 800 or h < 800:
                issues.append("render_too_small")
            extrema = img.convert("RGB").getextrema()
            if all((mx - mn) < 8 for mn, mx in extrema):
                issues.append("render_almost_blank")
    except Exception as exc:
        issues.append(f"render_unreadable:{exc}")

    expected = int(render.get("expected_block_count") or 0)
    placed = int(render.get("placed_block_count") or len(render.get("placed_blocks") or []))
    if expected and placed < expected:
        issues.append(f"missing_blocks:{placed}/{expected}")
    if render.get("issues"):
        issues.extend([str(i) for i in render.get("issues") or []])
    return issues


def _build_prompt(payload, render: Dict[str, Any]) -> str:
    return f"""
Ты — QA-инспектор технического черновика медицинской инфографики.

Контекст: это Stage 9 IDEAL SEMANTIC-LAYOUT RECONSTRUCTION PIPELINE v2.
Python НЕ рисует текст и дизайн на этом этапе. Он только раскладывает готовые PNG-компоненты по final_layout_blueprint. Твоя задача — проверить только технический черновик.

Проверь:
1. Все обязательные PNG-компоненты присутствуют.
2. Нет обрезания компонентов.
3. Нет наложений, которые мешают чтению.
4. Композиция понятна и иерархична.
5. Отступы и spacing выглядят безопасно.
6. Нет компонентов за пределами canvas.
7. Нет явных потерь смысла по strict_contract.
8. Если нужно исправить — предложи только layout_repairs: изменения размеров, x/y, spacing, canvas.
9. НЕ предлагай менять текст, медицинские факты или генерировать новые компоненты.

CONTINUATION PACKAGE:
{json.dumps(payload.continuation_package.model_dump(), ensure_ascii=False)[:12000]}

FINAL LAYOUT BLUEPRINT:
{json.dumps(payload.layout_blueprint or {}, ensure_ascii=False)[:12000]}

RENDER MANIFEST:
{json.dumps(render, ensure_ascii=False)[:12000]}

Верни строго JSON:
{{
  "draft_ok": true,
  "score": 0.0,
  "problems": [],
  "layout_repairs": [],
  "recommendation": "polish | repair_layout | use_draft | failed"
}}
""".strip()


def run_draft_qa(db: Session, state_id: int) -> DraftQAResult:
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    render = _latest_render(payload)
    if not render:
        raise DraftQAError("No technical render found. Run /render/technical first.")

    basic_issues = _basic_render_checks(render, payload)
    client = _get_client()
    ai_data: Dict[str, Any] = {}
    problems: List[str] = list(basic_issues)

    path = str(render.get("render_path") or "")
    if client is not None and Path(path).exists():
        try:
            response = client.responses.create(
                model=settings.openai_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _build_prompt(payload, render)},
                            {"type": "input_image", "image_url": f"data:image/png;base64,{_image_b64(path)}"},
                        ],
                    }
                ],
            )
            ai_data = _safe_json(response.output_text)
        except Exception as exc:
            problems.append(f"draft_qa_model_error:{type(exc).__name__}:{str(exc)[:180]}")
    else:
        problems.append("draft_qa_ai_skipped:no_openai_key")

    ai_problems = ai_data.get("problems") if isinstance(ai_data.get("problems"), list) else []
    problems.extend([str(p) for p in ai_problems])
    draft_ok = bool(ai_data.get("draft_ok")) and not basic_issues
    score = float(ai_data.get("score") or (1.0 if draft_ok else 0.3))
    repairs = ai_data.get("layout_repairs") if isinstance(ai_data.get("layout_repairs"), list) else []
    recommendation = str(ai_data.get("recommendation") or ("polish" if draft_ok else "repair_layout"))
    if recommendation not in {"polish", "repair_layout", "use_draft", "failed"}:
        recommendation = "polish" if draft_ok else "repair_layout"
    if basic_issues and recommendation == "polish":
        recommendation = "repair_layout"

    qa_item = {
        "render_id": render.get("render_id"),
        "render_path": path,
        "draft_ok": draft_ok,
        "score": score,
        "problems": problems,
        "layout_repairs": repairs,
        "recommendation": recommendation,
    }
    status = payload.component_status or {}
    status.setdefault("draft_qa", []).append(qa_item)
    status["latest_draft_qa"] = qa_item
    payload.component_status = status

    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="draft_qa",
        payload=payload,
        stage_result={"stage": "draft_qa", "draft_ok": draft_ok, "recommendation": recommendation, "problems": problems},
    )
    qa_history = status.get("draft_qa") if isinstance(status.get("draft_qa"), list) else []
    return DraftQAResult(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        render_id=str(render.get("render_id") or "") or None,
        draft_ok=draft_ok,
        status="ok" if draft_ok else "needs_repair",
        score=score,
        problems=problems,
        layout_repairs=repairs,
        recommendation=recommendation,  # type: ignore[arg-type]
        checked_render_path=path,
        qa_history_count=len(qa_history),
    )
