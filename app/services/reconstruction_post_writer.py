from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContentPost
from app.prompts.post_from_reconstruction import (
    POST_FROM_RECONSTRUCTION_SYSTEM_PROMPT,
    POST_FROM_RECONSTRUCTION_USER_TEMPLATE,
)
from app.schemas.reconstruction_post import ReconstructionPostResult
from app.services.project_state_manager import get_payload, get_project_state, update_project_state


class ReconstructionPostError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ReconstructionPostError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _json(data: Any) -> str:
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    return json.dumps(data or {}, ensure_ascii=False, indent=2)


def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            return {}
    return {}


def _latest_final_qa(payload) -> Dict[str, Any] | None:
    status = payload.component_status or {}
    item = status.get("latest_final_qa")
    return item if isinstance(item, dict) else None


def _image_data_url(path: str) -> str:
    data = Path(path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}"



def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        # Some prompts may return dictionaries like {"items": [...]} or {"danger_signs": [...]}
        for key in ("items", "list", "values", "danger_signs", "warning_signs", "safe_actions", "prevention"):
            if isinstance(value.get(key), list):
                return value.get(key) or []
        return [value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _walk_collect(data: Any, keys: set[str], limit: int = 24) -> List[str]:
    """Collect short text/list values by semantic key names from nested state."""
    results: List[str] = []

    def norm_key(k: str) -> str:
        return k.lower().replace("-", "_").replace(" ", "_")

    def add_value(v: Any) -> None:
        for item in _as_list(v):
            if isinstance(item, dict):
                text = item.get("text") or item.get("title") or item.get("name") or item.get("description") or item.get("value")
                if text:
                    results.append(str(text).strip())
                else:
                    compact = json.dumps(item, ensure_ascii=False)
                    if len(compact) <= 220:
                        results.append(compact)
            elif item is not None:
                txt = str(item).strip()
                if txt:
                    results.append(txt)
            if len(results) >= limit:
                return

    def walk(obj: Any) -> None:
        if len(results) >= limit:
            return
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump()
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = norm_key(str(k))
                if nk in keys:
                    add_value(v)
                walk(v)
                if len(results) >= limit:
                    return
        elif isinstance(obj, list):
            for x in obj:
                walk(x)
                if len(results) >= limit:
                    return

    walk(data)
    # de-duplicate preserving order
    seen = set()
    deduped = []
    for x in results:
        if x and x not in seen:
            seen.add(x)
            deduped.append(x)
    return deduped[:limit]


def _extract_post_facts_from_state(payload, final_qa: Dict[str, Any], final_image_path: str | None) -> Dict[str, Any]:
    """Create a compact, explicit fact pack for post generation.

    The model receives the full state too, but this field is the primary source for the post.
    It prevents warning signs / safe actions / prevention from being buried too deep inside state.
    """
    continuation = payload.continuation_package.model_dump() if payload.continuation_package else {}
    state_bundle = {
        "post_brief": payload.post_brief or {},
        "analysis_state": payload.analysis_state or {},
        "final_units": payload.final_units or [],
        "continuation_package": continuation,
        "final_qa": final_qa or {},
    }

    warning_keys = {
        "warning_signs", "danger_signs", "red_flags", "urgent_signs", "when_to_see_doctor",
        "when_to_seek_medical_help", "medical_warnings", "doctor_warning", "seek_help_if",
        "когда_к_врачу", "тревожные_признаки", "опасные_симптомы",
    }
    action_keys = {
        "safe_actions", "first_aid", "what_to_do", "next_steps", "patient_actions",
        "self_care", "safe_steps", "что_делать", "первая_помощь", "безопасные_действия",
    }
    prevention_keys = {
        "prevention", "preventive_actions", "how_to_prevent", "avoidance", "prophylaxis",
        "профилактика", "как_избежать",
    }
    limitation_keys = {
        "limitations", "disclaimer", "medical_disclaimer", "diagnostic_limitations", "must_avoid",
        "ограничения", "дисклеймер",
    }
    cta_keys = {"cta", "cta_strategy", "call_to_action", "next_action", "призыв"}

    post_brief = payload.post_brief or {}
    analysis = payload.analysis_state or {}
    title = (
        post_brief.get("title")
        or post_brief.get("post_title")
        or post_brief.get("final_title")
        or analysis.get("final_title")
        or analysis.get("topic")
        or ""
    )
    what_shows = (
        post_brief.get("what_infographic_shows")
        or post_brief.get("summary")
        or analysis.get("content_summary")
        or analysis.get("topic")
        or ""
    )

    final_unit_names: List[str] = []
    for unit in payload.final_units or []:
        if isinstance(unit, dict):
            name = unit.get("title") or unit.get("name") or unit.get("final_unit") or unit.get("label") or unit.get("id")
            if name:
                final_unit_names.append(str(name))
        elif unit:
            final_unit_names.append(str(unit))

    facts = {
        "topic": analysis.get("topic") or post_brief.get("topic") or title,
        "final_title": title,
        "what_infographic_shows": what_shows,
        "final_units": final_unit_names[:20],
        "limitations": _walk_collect(state_bundle, limitation_keys, limit=12),
        "medical_warnings": _walk_collect(state_bundle, warning_keys, limit=16),
        "safe_actions": _walk_collect(state_bundle, action_keys, limit=16),
        "prevention": _walk_collect(state_bundle, prevention_keys, limit=16),
        "cta_strategy": _walk_collect(state_bundle, cta_keys, limit=6),
        "final_image_path": final_image_path,
        "final_qa_summary": {
            "final_ok": final_qa.get("final_ok"),
            "use_image": final_qa.get("use_image"),
            "issues": final_qa.get("issues") or final_qa.get("problems") or [],
        },
        "post_generation_rules": [
            "Use this POST_FACTS block as the main source.",
            "Do not add medical claims that are absent from POST_FACTS or project_state.",
            "If medical_warnings/safe_actions/prevention are non-empty, include them explicitly.",
        ],
    }
    return facts


def _fallback_post(payload, final_image_path: str | None, issues: List[str]) -> Dict[str, Any]:
    post_brief = payload.post_brief or {}
    analysis = payload.analysis_state or {}
    title = (
        post_brief.get("title")
        or post_brief.get("post_title")
        or analysis.get("final_title")
        or analysis.get("topic")
        or "Полезная медицинская памятка"
    )
    final_units = payload.final_units or []
    unit_names = []
    for unit in final_units:
        if isinstance(unit, dict):
            name = unit.get("title") or unit.get("name") or unit.get("final_unit") or unit.get("id")
            if name:
                unit_names.append(str(name))
    body_parts = [
        f"{title}\n",
        "Мы подготовили инфографику, которая поможет быстро сориентироваться в теме и сохранить важные подсказки под рукой.",
    ]
    if unit_names:
        body_parts.append("В памятке разобраны: " + ", ".join(unit_names[:12]) + ".")
    body_parts.append("Важно: инфографика не заменяет консультацию врача и не предназначена для самостоятельной постановки диагноза.")
    body_parts.append("Если симптомы усиливаются, появляются тревожные признаки или состояние вызывает сомнения — лучше обратиться к специалисту.")
    return {
        "post_title": str(title),
        "post_text": "\n\n".join(body_parts),
        "cta": "Сохраните памятку и обращайтесь к врачу при ухудшении самочувствия.",
        "safety_notes": ["fallback_generated", *issues],
        "used_final_image_path": final_image_path,
        "source_fields_used": ["post_brief", "final_units", "analysis_state"],
    }


def _build_user_prompt(state, payload, final_qa: Dict[str, Any], final_image_path: str | None, post_facts: Dict[str, Any]) -> str:
    continuation = payload.continuation_package.model_dump()
    strict_contract = continuation.get("strict_contract") or {}
    return POST_FROM_RECONSTRUCTION_USER_TEMPLATE.format(
        state_id=state.id,
        pipeline_stage=state.pipeline_stage,
        state_version=state.state_version,
        final_image_path=final_image_path or "",
        post_facts_json=_json(post_facts),
        final_qa_json=_json(final_qa),
        post_brief_json=_json(payload.post_brief),
        final_units_json=_json(payload.final_units),
        analysis_state_json=_json(payload.analysis_state),
        continuation_package_json=_json(continuation),
        strict_contract_json=_json(strict_contract),
    )


def generate_post_from_reconstruction_state(db: Session, state_id: int, *, platform: str = "telegram") -> ReconstructionPostResult:
    """Stage 12: create blog/social post from final reconstruction state.

    The post is generated from persistent project state, not from a loose topic.
    It uses final QA, final_units, post_brief, continuation package and selected final image.
    """
    state = get_project_state(db, state_id)
    payload = get_payload(state)
    final_qa = _latest_final_qa(payload)
    issues: List[str] = []
    if not final_qa:
        raise ReconstructionPostError("Final QA is missing. Run /final/qa before post generation.")
    if not bool(final_qa.get("final_ok")):
        raise ReconstructionPostError("Final QA is not successful. Fix final image or use technical draft before post generation.")
    final_image_path = final_qa.get("final_image_path") or final_qa.get("technical_draft_path")
    post_facts = _extract_post_facts_from_state(payload, final_qa, str(final_image_path) if final_image_path else None)

    prompt = _build_user_prompt(state, payload, final_qa, final_image_path, post_facts)
    post_data: Dict[str, Any] = {}
    try:
        client = _get_client()
        content: List[Dict[str, Any]] = [
            {"type": "input_text", "text": POST_FROM_RECONSTRUCTION_SYSTEM_PROMPT},
            {"type": "input_text", "text": prompt},
        ]
        if final_image_path and Path(str(final_image_path)).exists():
            content.append({"type": "input_image", "image_url": _image_data_url(str(final_image_path))})
        response = client.responses.create(
            model=settings.openai_model,
            input=[{"role": "user", "content": content}],
        )
        text = getattr(response, "output_text", "") or ""
        post_data = _extract_json(text)
        if not post_data:
            issues.append("post_generation_json_parse_failed")
            post_data = _fallback_post(payload, str(final_image_path) if final_image_path else None, issues)
    except Exception as exc:
        issues.append(f"post_generation_ai_failed:{type(exc).__name__}:{str(exc)[:220]}")
        post_data = _fallback_post(payload, str(final_image_path) if final_image_path else None, issues)

    post_title = str(post_data.get("post_title") or "Медицинская памятка").strip()
    post_text = str(post_data.get("post_text") or "").strip()
    cta = str(post_data.get("cta") or "").strip()
    full_text = post_text
    if cta and cta not in full_text:
        full_text = f"{full_text}\n\n{cta}".strip()

    if not full_text:
        post_data = _fallback_post(payload, str(final_image_path) if final_image_path else None, [*issues, "empty_post_text"])
        post_title = str(post_data.get("post_title") or "Медицинская памятка")
        full_text = str(post_data.get("post_text") or "")
        cta = str(post_data.get("cta") or "")
        if cta and cta not in full_text:
            full_text = f"{full_text}\n\n{cta}".strip()

    content_post = ContentPost(
        title=post_title[:255],
        headline=post_title[:255],
        platform=platform,
        status="draft",
        text=full_text,
        ai_model=settings.openai_model,
        image_path=str(final_image_path) if final_image_path else None,
    )
    db.add(content_post)
    db.commit()
    db.refresh(content_post)

    record = {
        "post_id": content_post.id,
        "post_title": post_title,
        "post_text": full_text,
        "cta": cta,
        "final_image_path": str(final_image_path) if final_image_path else None,
        "issues": issues,
        "post_data": post_data,
        "post_facts": post_facts,
    }
    payload.component_status.setdefault("post_generation", []).append(record)
    payload.component_status["latest_post_generation"] = record
    payload.continuation_package.current_state_summary = "Post draft generated from final reconstruction state."
    payload.continuation_package.strict_contract.setdefault("post_generation", {})
    payload.continuation_package.strict_contract["post_generation"] = {
        "post_id": content_post.id,
        "post_facts_used": True,
        "must_be_based_on_state": True,
        "final_image_path": str(final_image_path) if final_image_path else None,
        "avoid_unsourced_medical_claims": True,
    }
    payload.continuation_package.next_step_prompt = (
        "Next step: review the generated post draft, edit if needed, approve and publish through the existing post workflow."
    )
    new_state = update_project_state(
        db,
        state_id,
        pipeline_stage="post_generation",
        payload=payload,
        stage_result={"stage": "post_generation", **record},
    )

    return ReconstructionPostResult(
        project_state_id=state_id,
        pipeline_stage=new_state.pipeline_stage,
        state_version=new_state.state_version,
        post_id=content_post.id,
        post_title=post_title,
        post_text=full_text,
        cta=cta,
        final_image_path=str(final_image_path) if final_image_path else None,
        status="draft_created",
        issues=issues,
        post_record=record,
    )
