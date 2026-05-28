"""Source Unit Decision Engine v2.

This engine implements the decision layer from the IDEAL SEMANTIC-LAYOUT
RECONSTRUCTION PIPELINE v2.

Important design principles:
- It is universal: no hard-coded examples, organisms, diseases, shapes or layouts.
- The AI must make a decision, not merely rate relevance.
- Every source unit gets exactly one decision: keep | remove | replace | merge.
- replace requires a reference_unit so later generated replacements inherit style.
- remove is preferred when no good replacement exists.
- final_units are built from decisions, not copied mechanically from source_units.
"""
from __future__ import annotations

import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image


AUDIENCE_CONTEXT = "Россия / Москва / Средняя полоса России; русскоязычная аудитория медицинской клиники"
ALLOWED_DECISIONS = {"keep", "merge", "replace", "remove"}
ALLOWED_POLICIES = {"use_reference_and_clean", "replace_with_new", "generate_new", "preserve_from_reference"}


def _image_data_url(image: Image.Image) -> str:
    buf = BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def _safe_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _cards_for_prompt(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, c in enumerate(cards, start=1):
        out.append({
            "idx": idx,
            "id": c.get("id") or c.get("unit_id") or c.get("source_unit_id") or f"unit_{idx}",
            "title": c.get("title") or c.get("label_ru") or c.get("source_label") or c.get("new_element") or c.get("visual_element") or f"Unit {idx}",
            "type": c.get("type") or c.get("unit_role") or c.get("unit_type"),
            "source_policy": c.get("source_policy"),
            "old_element": c.get("old_element"),
            "new_element": c.get("new_element"),
            "visual_element": c.get("visual_element"),
            "lines": c.get("lines"),
            "components": c.get("components"),
        })
    return out


def decide_source_units_with_ai(
    *,
    client: OpenAI,
    model: str,
    source_image: Image.Image,
    cards: list[dict[str, Any]],
    contract_summary_text: str = "",
    audience_context: str = AUDIENCE_CONTEXT,
) -> dict[str, Any]:
    """Return explicit source-unit decisions in strict JSON.

    This function is kept for legacy branches that still call a separate source-unit
    decision step. Newer master reconstruction should already include the same
    data in ProjectStatePayload.unit_decisions/final_units.
    """
    prompt = f"""
Ты — медицинский и региональный редактор инфографик.

Твоя задача — НЕ делать crop и НЕ генерировать картинку. Твоя задача — принять
окончательное решение по каждому элементу исходной инфографики для аудитории:
{audience_context}.

Входные source_units/cards:
{json.dumps(_cards_for_prompt(cards), ensure_ascii=False, indent=2)}

Контракт реконструкции:
{contract_summary_text}

КРИТИЧЕСКОЕ ПРАВИЛО УНИВЕРСАЛЬНОСТИ:
- Не используй hardcoded examples.
- Не ориентируйся на конкретный пример, тему, форму, круги, карточки или сетки.
- Работай одинаково с любой медицинской инфографикой.

ОБЯЗАТЕЛЬНЫЙ АЛГОРИТМ ДЛЯ КАЖДОГО source_unit:
1. Определи, что это за смысловая единица.
2. Определи, полезна ли она медицински для новой инфографики.
3. Определи, подходит ли она аудитории: Россия / Москва / Средняя полоса России.
4. Определи, дублирует ли она другой source_unit.
5. Прими ОДНО решение: keep | merge | replace | remove.

РЕШЕНИЯ:
- keep: оставить, если элемент медицински полезен, понятен и уместен.
- merge: объединить с близкой категорией, если отдельный элемент не нужен.
- replace: заменить, если элемент нерелевантен/устарел/неподходящий, НО есть хорошая более подходящая альтернатива.
- remove: убрать, если элемент не нужен и хорошей альтернативы нет.

ВАЖНО ДЛЯ replace:
- replacement_title обязателен.
- reference_unit_id обязателен.
- reference_unit — это final/source unit, от которого generated replacement должен унаследовать стиль.
- Укажи style_inheritance_rules: форму, масштаб, цветовую логику, стиль иллюстрации, освещение, толщину линий, визуальную иерархию.

ЗАПРЕЩЕНО:
- возвращать только оценки high/medium/low вместо решения;
- оставлять source_unit без решения;
- механически копировать все source_units в final_units;
- оставлять нерелевантный элемент только потому, что он есть в исходнике;
- использовать конкретные частные замены как общее правило.

Верни только JSON:
{{
  "audience_context": "...",
  "source_unit_decisions": [
    {{
      "source_id": "id из входа или unit_N",
      "source_title": "...",
      "translated_title": "...",
      "decision": "keep|merge|replace|remove",
      "has_good_alternative": true,
      "final_unit_id": "...|null",
      "final_title": "...|null",
      "replacement_title": "...|null",
      "reference_unit_id": "...|null",
      "style_inheritance_rules": {{
        "inherit_from_reference": true,
        "preserve_style_features": ["shape", "scale", "color logic", "illustration style", "lighting", "line weight", "visual hierarchy"]
      }},
      "merge_target_unit_id": "...|null",
      "reason": "четкое медицинское/региональное/смысловое объяснение"
    }}
  ],
  "final_units": [
    {{
      "final_unit_id": "...",
      "title": "...",
      "source_policy": "use_reference_and_clean|replace_with_new|generate_new",
      "source_ids": ["..."],
      "old_elements": ["..."],
      "new_element": "...|null",
      "reference_unit_id": "...|null",
      "style_inheritance_rules": {{}},
      "reason": "..."
    }}
  ],
  "required_elements": ["..."],
  "forbidden_elements": ["только реальные видимые объекты, которые нельзя показывать"],
  "replacement_rules": [
    {{"remove": "...", "add": "...", "reference_unit_id": "...", "reason": "..."}}
  ],
  "merge_rules": [
    {{"sources": ["..."], "target": "...", "reason": "..."}}
  ],
  "notes": ["..."]
}}
""".strip()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "Ты принимаешь окончательные keep/merge/replace/remove решения для медицинской инфографики. Отвечай только JSON."},
            {"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": _image_data_url(source_image)},
            ]},
        ],
    )
    return _safe_json(response.output_text)


def _find_card(cards: list[dict[str, Any]], source_id: str, source_title: str = "") -> dict[str, Any] | None:
    sid = _norm(source_id)
    st = _norm(source_title)
    for idx, c in enumerate(cards, start=1):
        keys = {
            _norm(c.get("id") or c.get("unit_id") or c.get("source_unit_id") or f"unit_{idx}"),
            _norm(c.get("title")),
            _norm(c.get("label_ru")),
            _norm(c.get("source_label")),
            _norm(c.get("visual_element")),
            _norm(c.get("new_element")),
            f"unit_{idx}",
            str(idx),
        }
        if sid and sid in keys:
            return c
        if st and st in keys:
            return c
    return None


def _decision_by_source(decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in decisions.get("source_unit_decisions") or decisions.get("unit_decisions") or []:
        if not isinstance(d, dict):
            continue
        key = _norm(d.get("source_id") or d.get("source_unit_id") or d.get("source_label") or d.get("source_title"))
        if key:
            out[key] = d
    return out


def apply_source_unit_decisions(cards: list[dict[str, Any]], decisions: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Create final cards from explicit decisions.

    Conservative on malformed JSON: returns original cards plus issues if the
    decision contract is unusable.
    """
    issues = validate_source_unit_decisions(decisions)
    if not isinstance(decisions, dict):
        return cards, issues
    final_units = decisions.get("final_units")
    if not isinstance(final_units, list) or not final_units:
        return cards, issues or ["source_unit_decisions_no_final_units"]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, fu in enumerate(final_units, start=1):
        if not isinstance(fu, dict):
            continue
        title = str(fu.get("title") or fu.get("label_ru") or fu.get("new_element") or f"Unit {idx}").strip()
        if not title:
            continue
        final_id = str(fu.get("final_unit_id") or fu.get("id") or f"unit_{idx}").strip()
        key = _norm(final_id or title)
        if key in seen:
            issues.append(f"source_unit_duplicate_final_unit:{title}")
            continue
        seen.add(key)

        source_ids = fu.get("source_ids") if isinstance(fu.get("source_ids"), list) else []
        base = None
        for sid in source_ids:
            base = _find_card(cards, str(sid), "")
            if base:
                break
        if base is None:
            base = _find_card(cards, str(fu.get("final_unit_id") or ""), title)

        card = dict(base) if base else {"type": "comparison_card", "lines": []}
        card["id"] = final_id
        card["title"] = title
        card["number"] = idx
        card["source_policy"] = str(fu.get("source_policy") or card.get("source_policy") or "generate_new")
        card["source_ids"] = source_ids
        card["reference_unit_id"] = fu.get("reference_unit_id")
        card["style_inheritance_rules"] = fu.get("style_inheritance_rules") or {}
        if fu.get("new_element"):
            card["new_element"] = fu.get("new_element")
            card.setdefault("visual_element", fu.get("new_element"))
        if fu.get("old_elements"):
            card["old_element"] = ", ".join(str(x) for x in fu.get("old_elements") if str(x).strip())
        if card["source_policy"] in {"replace_with_new", "generate_new"}:
            card.pop("source_bbox", None)
            card.pop("source_location_hint", None)
        card["source_unit_decision_reason"] = fu.get("reason")
        out.append(card)

    if len(out) < 2:
        return cards, issues + ["source_unit_decisions_too_few_final_units"]
    return out, issues


def validate_source_unit_decisions(decisions: dict[str, Any]) -> list[str]:
    """Validate explicit source-unit decisions without relying on high/low ratings."""
    issues: list[str] = []
    if not isinstance(decisions, dict):
        return ["source_unit_decisions_invalid"]

    source_decisions = decisions.get("source_unit_decisions") or decisions.get("unit_decisions")
    if not isinstance(source_decisions, list) or not source_decisions:
        issues.append("source_unit_no_source_decisions")
        return issues

    seen_sources: set[str] = set()
    decision_targets: dict[str, list[str]] = {}
    for idx, d in enumerate(source_decisions, start=1):
        if not isinstance(d, dict):
            issues.append(f"source_unit_decision_invalid:{idx}")
            continue
        source_id = str(d.get("source_id") or d.get("source_unit_id") or d.get("source_title") or d.get("source_label") or f"unit_{idx}").strip()
        key = _norm(source_id)
        if key in seen_sources:
            issues.append(f"source_unit_duplicate_decision:{source_id}")
        seen_sources.add(key)
        decision = str(d.get("decision") or "").lower().strip()
        if decision not in ALLOWED_DECISIONS:
            issues.append(f"source_unit_missing_or_invalid_decision:{source_id}")
            continue
        final_id = str(d.get("final_unit_id") or d.get("final_title") or d.get("final_label_ru") or d.get("replacement_title") or d.get("merge_target_unit_id") or "").strip()
        if decision == "keep" and not final_id:
            issues.append(f"keep_missing_final_unit:{source_id}")
        if decision == "merge":
            target = str(d.get("merge_target_unit_id") or d.get("final_unit_id") or d.get("final_title") or "").strip()
            if not target:
                issues.append(f"merge_missing_target:{source_id}")
        if decision == "replace":
            if not (d.get("replacement_title") or d.get("final_title") or d.get("final_label_ru")):
                issues.append(f"replace_missing_replacement_title:{source_id}")
            if not d.get("reference_unit_id"):
                issues.append(f"replace_missing_reference_unit:{source_id}")
            style_rules = d.get("style_inheritance_rules")
            if not isinstance(style_rules, dict) or not style_rules:
                issues.append(f"replace_missing_style_inheritance_rules:{source_id}")
        if decision == "remove" and d.get("has_good_alternative") is True:
            issues.append(f"remove_but_good_alternative_true:{source_id}")
        if final_id:
            decision_targets.setdefault(_norm(final_id), []).append(source_id)

    final_units = decisions.get("final_units")
    if not isinstance(final_units, list) or not final_units:
        issues.append("source_unit_no_final_units")
        return issues

    final_ids: set[str] = set()
    for idx, fu in enumerate(final_units, start=1):
        if not isinstance(fu, dict):
            issues.append(f"source_unit_final_invalid:{idx}")
            continue
        final_id = str(fu.get("final_unit_id") or fu.get("id") or fu.get("title") or idx).strip()
        title = fu.get("title") or fu.get("label_ru")
        if not title:
            issues.append(f"source_unit_final_missing_title:{idx}")
        if _norm(final_id) in final_ids:
            issues.append(f"source_unit_duplicate_final_id:{final_id}")
        final_ids.add(_norm(final_id))
        policy = str(fu.get("source_policy") or "").strip()
        if policy and policy not in ALLOWED_POLICIES:
            issues.append(f"source_unit_final_bad_policy:{title or final_id}")
        source_ids = fu.get("source_ids")
        if not isinstance(source_ids, list):
            issues.append(f"source_unit_final_missing_source_ids:{title or final_id}")
        if policy == "replace_with_new" and not fu.get("reference_unit_id"):
            issues.append(f"source_unit_final_replace_missing_reference:{title or final_id}")
        if policy == "replace_with_new" and not fu.get("style_inheritance_rules"):
            issues.append(f"source_unit_final_replace_missing_style_rules:{title or final_id}")

    # Every non-remove decision must be represented in final_units.
    represented_sources = {
        _norm(sid)
        for fu in final_units if isinstance(fu, dict)
        for sid in (fu.get("source_ids") if isinstance(fu.get("source_ids"), list) else [])
    }
    for d in source_decisions:
        if not isinstance(d, dict):
            continue
        decision = str(d.get("decision") or "").lower().strip()
        sid = _norm(d.get("source_id") or d.get("source_unit_id") or d.get("source_title") or d.get("source_label"))
        if decision in {"keep", "merge", "replace"} and sid and sid not in represented_sources:
            # For merge/replace, source can be represented indirectly by target; accept if final_unit_id exists.
            target = _norm(d.get("final_unit_id") or d.get("final_title") or d.get("replacement_title") or d.get("merge_target_unit_id"))
            has_target = any(_norm(fu.get("final_unit_id") or fu.get("title")) == target for fu in final_units if isinstance(fu, dict))
            if not has_target:
                issues.append(f"decision_not_represented_in_final_units:{sid}")

    return issues
