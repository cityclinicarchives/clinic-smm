from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.final_qa import FinalQAResult
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class FinalQAError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise FinalQAError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _latest_render(payload) -> Dict[str, Any] | None:
    history = payload.render_history or []
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("render_path"):
                return item
    return None


def _latest_polish(payload) -> Dict[str, Any] | None:
    status = payload.component_status or {}
    item = status.get("latest_design_polish")
    return item if isinstance(item, dict) else None


def _image_data_url(path: str) -> str:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _extract_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _build_final_qa_prompt(payload, render: Dict[str, Any], polish: Dict[str, Any] | None) -> str:
    continuation = payload.continuation_package.model_dump()
    contract = continuation.get("strict_contract") or {}
    return f"""
Ты — final QA reviewer медицинской SMM-системы.

Тебе нужно сравнить polished image с technical draft и строгим контрактом проекта.

Технический черновик считается fallback-истиной: если polished image потерял блоки, изменил текст, изменил медицинский смысл, добавил запрещенные элементы или испортил layout, нужно выбрать technical_draft.

Проверить обязательно:
1. Все ли semantic/final units из контракта присутствуют.
2. Не удалены ли блоки и не изменен ли порядок важных элементов.
3. Не изменен ли текст в PNG-компонентах.
4. Нет ли искаженной кириллицы.
5. Нет ли запрещенных объектов, watermark, интерфейса соцсетей.
6. Не изменены ли медицинские факты и предупреждения.
7. Не обрезаны ли компоненты.
8. Читаема ли композиция.

STRICT CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

CONTINUATION PACKAGE:
{json.dumps(continuation, ensure_ascii=False, indent=2)}

TECHNICAL RENDER MANIFEST:
{json.dumps(render, ensure_ascii=False, indent=2)}

DESIGN POLISH RECORD:
{json.dumps(polish or {{}}, ensure_ascii=False, indent=2)}

Верни строго JSON:
{{
  "final_ok": true,
  "use_image": "polished | technical_draft | none",
  "score": 0.0,
  "problems": [],
  "reason": "..."
}}
""".strip()


def _heuristic_final_qa(payload, render: Dict[str, Any], polish: Dict[str, Any] | None, problems: Optional[List[str]] = None) -> Dict[str, Any]:
    problems = list(problems or [])
    render_path = str(render.get("render_path") or "")
    polished_path = str((polish or {}).get("polished_path") or "")

    if not render_path or not Path(render_path).exists():
        return {"final_ok": False, "use_image": "none", "score": 0.0, "problems": problems + ["technical_draft_missing"]}

    if not polish or polish.get("status") != "ready" or not polished_path or not Path(polished_path).exists():
        return {"final_ok": True, "use_image": "technical_draft", "score": 0.75, "problems": problems + ["polish_missing_or_failed"]}

    # If no AI QA is available, prefer technical draft for safety.
    return {"final_ok": True, "use_image": "technical_draft", "score": 0.70, "problems": problems + ["ai_final_qa_unavailable_fallback_to_draft"]}


def run_final_qa(db: Session, state_id: int) -> FinalQAResult:
    """Stage 11: compare polished image against technical draft and strict contract.

    The polished image is optional. If QA is uncertain or fails, the technical
    draft is selected as the safe fallback.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    render = _latest_render(payload)
    if not render:
        raise FinalQAError("No technical render found. Run /render/technical first.")
    render_path = str(render.get("render_path") or "")
    if not render_path or not Path(render_path).exists():
        raise FinalQAError("Technical render file is missing.")

    polish = _latest_polish(payload)
    polished_path = str((polish or {}).get("polished_path") or "")
    problems: List[str] = []

    qa_data: Dict[str, Any]
    if polish and polish.get("status") == "ready" and polished_path and Path(polished_path).exists():
        prompt = _build_final_qa_prompt(payload, render, polish)
        try:
            client = _get_client()
            response = client.responses.create(
                model=settings.openai_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_text", "text": "TECHNICAL DRAFT:"},
                            {"type": "input_image", "image_url": _image_data_url(render_path)},
                            {"type": "input_text", "text": "POLISHED IMAGE:"},
                            {"type": "input_image", "image_url": _image_data_url(polished_path)},
                        ],
                    }
                ],
            )
            text = getattr(response, "output_text", "") or ""
            qa_data = _extract_json(text)
            if not qa_data:
                qa_data = _heuristic_final_qa(payload, render, polish, ["final_qa_json_parse_failed"])
        except Exception as exc:
            qa_data = _heuristic_final_qa(payload, render, polish, [f"final_qa_ai_failed:{type(exc).__name__}:{str(exc)[:180]}"])
    else:
        qa_data = _heuristic_final_qa(payload, render, polish, [])

    use_image = str(qa_data.get("use_image") or "technical_draft")
    if use_image not in {"polished", "technical_draft", "none"}:
        use_image = "technical_draft"

    if use_image == "polished" and not (polished_path and Path(polished_path).exists()):
        use_image = "technical_draft"
        problems.append("polished_selected_but_missing_fallback_to_draft")

    final_image_path = polished_path if use_image == "polished" else render_path if use_image == "technical_draft" else None
    final_ok = bool(qa_data.get("final_ok")) and use_image != "none"
    all_problems = list(qa_data.get("problems") or []) + problems
    score = float(qa_data.get("score") or 0.0)
    status = "passed" if final_ok and use_image == "polished" else "fallback_to_draft" if final_ok else "failed"

    record = {
        "final_ok": final_ok,
        "use_image": use_image,
        "final_image_path": final_image_path,
        "technical_draft_path": render_path,
        "polished_path": polished_path or None,
        "score": score,
        "problems": all_problems,
        "qa_data": qa_data,
    }
    payload.component_status.setdefault("final_qa", []).append(record)
    payload.component_status["latest_final_qa"] = record
    payload.continuation_package.current_state_summary = (
        "Final QA completed. Use selected final image for publication/post generation."
        if final_ok
        else "Final QA failed. Repair draft or rerun design polish before publication."
    )
    payload.continuation_package.strict_contract.setdefault("final_qa", {})
    payload.continuation_package.strict_contract["final_qa"] = {
        "selected_image": use_image,
        "final_image_path": final_image_path,
        "technical_draft_is_fallback": True,
        "polish_accepted": use_image == "polished",
    }
    payload.continuation_package.next_step_prompt = (
        "Next step: generate the blog/social post using post_brief, final_units, medical warnings, safe actions, "
        "prevention, and final_image_summary from this final QA state."
    )

    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="final_qa",
        payload=payload,
        stage_result={"stage": "final_qa", **record},
    )
    return FinalQAResult(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        status=status,  # type: ignore[arg-type]
        final_ok=final_ok,
        use_image=use_image,  # type: ignore[arg-type]
        final_image_path=final_image_path,
        technical_draft_path=render_path,
        polished_path=polished_path or None,
        score=score,
        problems=all_problems,
        qa_record=record,
    )
