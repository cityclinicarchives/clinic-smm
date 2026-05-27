"""Source Unit Decision Engine for infographic reconstruction.

This stage separates source units from final units. It performs the medical,
regional and semantic relevance check BEFORE crop planning, so crop/extraction
works only with the units that should actually appear in the final infographic.

The engine is intentionally conservative: if the AI decision step fails, it
returns the original cards unchanged with diagnostic notes.
"""
from __future__ import annotations

import base64
import json
import re
from io import BytesIO
from typing import Any

from openai import OpenAI
from PIL import Image


AUDIENCE_CONTEXT = "Россия, Москва; медицинская клиника; русскоязычная широкая аудитория"


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
            "id": c.get("id") or c.get("unit_id") or f"unit_{idx}",
            "title": c.get("title") or c.get("new_element") or c.get("visual_element") or f"Unit {idx}",
            "type": c.get("type"),
            "source_policy": c.get("source_policy"),
            "old_element": c.get("old_element"),
            "new_element": c.get("new_element"),
            "visual_element": c.get("visual_element"),
            "lines": c.get("lines"),
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
    """Return source/final unit decisions in strict JSON."""
    prompt = f"""
Ты — медицинский и региональный редактор инфографик.

Твоя задача — НЕ делать crop и НЕ генерировать картинку. Твоя задача — решить,
какие элементы исходной инфографики должны попасть в финальную инфографику для
аудитории: {audience_context}.

Входные карточки/единицы, которые уже нашла программа:
{json.dumps(_cards_for_prompt(cards), ensure_ascii=False, indent=2)}

Контракт реконструкции:
{contract_summary_text}

ОБЯЗАТЕЛЬНЫЙ АЛГОРИТМ ДЛЯ КАЖДОГО source_unit:
1. Определи, что это за объект/понятие.
2. Проверь медицинскую полезность для темы.
3. Проверь региональную/культурную актуальность для России и Москвы.
4. Проверь, дублирует ли он другой элемент.
5. Прими решение: keep | merge | replace | remove.
6. Если merge — укажи final_unit, в который он объединяется.
7. Если replace — укажи replacement_title и объясни, почему.
8. Если remove — объясни, почему элемент не нужен.

ВАЖНО:
- Не копируй source_units механически в final_units.
- final_units создаются заново на основе медицинской и региональной логики.
- Если элемент редкий/нерелевантный для России/Москвы, его можно заменить или удалить.
- Если есть два похожих элемента одной группы, не создавай дубль без веской причины.
- Примеры с конкретными насекомыми не являются правилами. Применяй общий принцип к любой теме.
- Если тема не про насекомых, всё равно применяй тот же алгоритм к объектам/симптомам/товарам/странам/флагам/вывескам и т.д.
- Для медицинской инфографики не обещай точную диагностику по фото.

Верни только JSON:
{{
  "audience_context": "...",
  "source_unit_decisions": [
    {{
      "source_id": "id из входа или unit_N",
      "source_title": "...",
      "translated_title": "...",
      "medical_relevance": "high|medium|low",
      "regional_relevance": "high|medium|low",
      "duplication_group": "...|none",
      "decision": "keep|merge|replace|remove",
      "final_unit_id": "...",
      "final_title": "...",
      "replacement_title": "...|null",
      "reason": "..."
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
      "reason": "..."
    }}
  ],
  "required_elements": ["..."],
  "forbidden_elements": ["только реальные видимые объекты, которые нельзя показывать"],
  "notes": ["..."]
}}
""".strip()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "Ты принимаешь решения keep/merge/replace/remove для медицинской инфографики. Отвечай только JSON."},
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
            _norm(c.get("id") or c.get("unit_id") or f"unit_{idx}"),
            _norm(c.get("title")),
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


def apply_source_unit_decisions(cards: list[dict[str, Any]], decisions: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Create final cards from AI decisions. Conservative on malformed JSON."""
    issues: list[str] = []
    if not isinstance(decisions, dict):
        return cards, ["source_unit_decisions_invalid"]
    final_units = decisions.get("final_units")
    if not isinstance(final_units, list) or not final_units:
        return cards, ["source_unit_decisions_no_final_units"]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, fu in enumerate(final_units, start=1):
        if not isinstance(fu, dict):
            continue
        title = str(fu.get("title") or fu.get("new_element") or f"Unit {idx}").strip()
        if not title:
            continue
        key = _norm(fu.get("final_unit_id") or title)
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
            # also try by title
            base = _find_card(cards, str(fu.get("final_unit_id") or ""), title)
        card = dict(base) if base else {"type": "comparison_card", "lines": []}
        card["id"] = str(fu.get("final_unit_id") or card.get("id") or f"unit_{idx}")
        card["title"] = title
        card["number"] = idx
        source_policy = str(fu.get("source_policy") or "").strip()
        if source_policy:
            card["source_policy"] = source_policy
        if fu.get("new_element"):
            card["new_element"] = fu.get("new_element")
            card.setdefault("visual_element", fu.get("new_element"))
        if fu.get("old_elements"):
            card["old_element"] = ", ".join(str(x) for x in fu.get("old_elements") if str(x).strip())
        if str(card.get("source_policy") or "").lower() in {"replace_with_new", "generate_new"}:
            card.pop("source_bbox", None)
            card.pop("source_location_hint", None)
        card["source_unit_decision_reason"] = fu.get("reason")
        out.append(card)
    if len(out) < 2:
        return cards, ["source_unit_decisions_too_few_final_units"]
    return out, issues


def validate_source_unit_decisions(decisions: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not isinstance(decisions, dict):
        return ["source_unit_decisions_invalid"]
    finals = decisions.get("final_units")
    if not isinstance(finals, list) or not finals:
        issues.append("source_unit_no_final_units")
    for idx, fu in enumerate(finals or [], start=1):
        if not isinstance(fu, dict):
            issues.append(f"source_unit_final_invalid:{idx}")
            continue
        if not fu.get("title"):
            issues.append(f"source_unit_final_missing_title:{idx}")
        if str(fu.get("source_policy") or "") not in {"use_reference_and_clean", "replace_with_new", "generate_new", "preserve_from_reference"}:
            issues.append(f"source_unit_final_bad_policy:{fu.get('title') or idx}")
    return issues
