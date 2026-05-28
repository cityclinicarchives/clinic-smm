"""v31 Reconstruction Contract.

This module turns high-level reconstruction decisions into a strict contract
that can be validated before/after image generation. It prevents cases like
"remove Scorpio, add horsefly" being described in prose but not executed.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _norm(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _text_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value or "")


def _blocks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    structure = spec.get("structure") or {}
    blocks = structure.get("blocks") or []
    result = [b for b in blocks if isinstance(b, dict)]
    atomic = spec.get("atomic_blueprint") or {}
    units = atomic.get("content_units") if isinstance(atomic, dict) else None
    if isinstance(units, list):
        # Atomic units are semantically important too; include them if not already present.
        for u in units:
            if isinstance(u, dict) and u not in result:
                result.append(u)
    return result


def get_reconstruction_contract(spec: dict[str, Any]) -> dict[str, Any]:
    """Return normalized contract object.

    Preferred schema is spec["reconstruction_contract"]. For older specs we also
    accept top-level required_elements / forbidden_elements / replacement_rules.
    """
    contract = spec.get("reconstruction_contract")
    if not isinstance(contract, dict):
        contract = {}

    for key in [
        "required_elements",
        "forbidden_elements",
        "replacement_rules",
        "required_blocks",
        "expected_atomic_cards",
        "expected_block_count",
    ]:
        if key not in contract and key in spec:
            contract[key] = spec.get(key)

    # Light inference from replacement rules that may live in source_analysis.what_to_replace.
    if not contract.get("replacement_rules"):
        source_analysis = spec.get("source_analysis") or {}
        replacements = []
        for item in _as_list(source_analysis.get("what_to_replace")):
            if isinstance(item, dict):
                replacements.append(item)
            else:
                text = str(item or "")
                # Parse common pattern: "заменить X на Y" / "X -> Y".
                m = re.search(r"(?:замен\w*\s+)?(.+?)\s*(?:->|→|на)\s*(.+?)(?:$|,|;|\.)", text, flags=re.IGNORECASE)
                if m and len(m.group(1)) < 60 and len(m.group(2)) < 80:
                    replacements.append({"remove": m.group(1).strip(), "add": m.group(2).strip(), "must_not_appear": True})
        if replacements:
            contract["replacement_rules"] = replacements

    # Required elements may be inferred from comparison cards if absent.
    if not contract.get("required_elements"):
        cards = [b for b in _blocks(spec) if str(b.get("type") or "").lower() in {"comparison_card", "card", "tile", "visual_card", "comparison_item"}]
        if cards:
            contract["required_elements"] = [b.get("title") or b.get("new_element") or b.get("visual_element") for b in cards if (b.get("title") or b.get("new_element") or b.get("visual_element"))]
            contract.setdefault("expected_atomic_cards", len(cards))

    # Replacement rules imply required + forbidden elements.
    forbidden = [str(x) for x in _as_list(contract.get("forbidden_elements")) if str(x).strip()]
    required = [str(x) for x in _as_list(contract.get("required_elements")) if str(x).strip()]
    for rule in _as_list(contract.get("replacement_rules")):
        if not isinstance(rule, dict):
            continue
        remove = rule.get("remove") or rule.get("old_element") or rule.get("source_item")
        add = rule.get("add") or rule.get("new_element") or rule.get("replacement")
        if remove and str(remove) not in forbidden:
            forbidden.append(str(remove))
        if add and str(add) not in required:
            required.append(str(add))
    contract["forbidden_elements"] = forbidden
    contract["required_elements"] = required
    return contract


def _contains_any(text: str, needles: list[str]) -> list[str]:
    ntext = _norm(text)
    found = []
    for needle in needles:
        n = _norm(needle)
        if n and n in ntext:
            found.append(needle)
    return found


def _content_text_for_final_units(spec: dict[str, Any]) -> str:
    """Text that represents final planned content, excluding source-analysis prose."""
    blocks = _blocks(spec)
    keep = []
    for b in blocks:
        # Only fields that are expected to appear in the FINAL visible output.
        # Do NOT include old_element, replacement_prompt, source_policy or must_avoid:
        # those are internal instructions and may legitimately contain removed/forbidden terms.
        keep.append({
            "id": b.get("id"),
            "type": b.get("type"),
            "title": b.get("title"),
            "lines": b.get("lines"),
            "visual_element": b.get("visual_element"),
            "new_element": b.get("new_element"),
        })
    return _text_blob({
        "title": spec.get("title"),
        "structure_blocks": keep,
        "visual_must_include": (spec.get("visual") or {}).get("must_include"),
    })


def validate_contract_on_spec(spec: dict[str, Any]) -> list[str]:
    """Validate semantic obligations before rendering.

    Returns issue codes/text. Any issue starting with contract_critical should
    stop rendering until blueprint is repaired.
    """
    issues: list[str] = []
    contract = get_reconstruction_contract(spec)
    blocks = _blocks(spec)
    cards = [b for b in blocks if str(b.get("type") or "").lower() in {"comparison_card", "card", "tile", "visual_card", "comparison_item"}]
    final_text = _content_text_for_final_units(spec)

    expected_cards = contract.get("expected_atomic_cards")
    try:
        expected_cards_i = int(expected_cards) if expected_cards is not None and str(expected_cards).strip() else None
    except Exception:
        expected_cards_i = None
    if expected_cards_i is not None and len(cards) < expected_cards_i:
        issues.append(f"contract_critical_expected_{expected_cards_i}_atomic_cards_but_found_{len(cards)}")

    expected_blocks = contract.get("expected_block_count")
    try:
        expected_blocks_i = int(expected_blocks) if expected_blocks is not None and str(expected_blocks).strip() else None
    except Exception:
        expected_blocks_i = None
    if expected_blocks_i is not None and len(blocks) < expected_blocks_i:
        issues.append(f"contract_critical_expected_{expected_blocks_i}_blocks_but_found_{len(blocks)}")

    required = [str(x) for x in _as_list(contract.get("required_elements")) if str(x).strip()]
    for item in required:
        if not _contains_any(final_text, [item]):
            issues.append(f"contract_critical_missing_required_element:{item}")

    forbidden = [str(x) for x in _as_list(contract.get("forbidden_elements")) if str(x).strip()]
    found_forbidden = _contains_any(final_text, forbidden)
    for item in found_forbidden:
        issues.append(f"contract_critical_forbidden_element_in_final_blueprint:{item}")

    # Replacement rules must be executable: add present, remove absent, replacement block exists.
    for idx, rule in enumerate(_as_list(contract.get("replacement_rules")), start=1):
        if not isinstance(rule, dict):
            continue
        remove = str(rule.get("remove") or rule.get("old_element") or rule.get("source_item") or "").strip()
        add = str(rule.get("add") or rule.get("new_element") or rule.get("replacement") or "").strip()
        if add and not _contains_any(final_text, [add]):
            issues.append(f"contract_critical_replacement_{idx}_missing_new_element:{add}")
        if remove and _contains_any(final_text, [remove]):
            issues.append(f"contract_critical_replacement_{idx}_old_element_still_present:{remove}")
        if add:
            matching_blocks = [b for b in blocks if _contains_any(_text_blob(b), [add])]
            if not matching_blocks:
                issues.append(f"contract_critical_replacement_{idx}_no_block_for_new_element:{add}")

    required_blocks = [str(x) for x in _as_list(contract.get("required_blocks")) if str(x).strip()]
    block_text = _text_blob([{"type": b.get("type"), "title": b.get("title"), "id": b.get("id")} for b in blocks])
    for rb in required_blocks:
        if not _contains_any(block_text, [rb]):
            issues.append(f"contract_missing_required_block:{rb}")

    # v32: post_brief is required for reconstruction posts so text generation
    # does not drift into a generic article unrelated to the final infographic.
    post_brief = spec.get("post_brief")
    if not isinstance(post_brief, dict) or not post_brief:
        issues.append("contract_missing_post_brief")
    else:
        must_have_any = ["must_include", "warning_signs", "safe_actions", "what_infographic_shows"]
        if not any(post_brief.get(k) for k in must_have_any):
            issues.append("contract_post_brief_too_weak")
    return issues


def contract_critical_issues(issues: list[str]) -> list[str]:
    return [x for x in issues if str(x).startswith("contract_critical")]


def contract_summary(spec: dict[str, Any]) -> str:
    contract = get_reconstruction_contract(spec)
    if not contract:
        return "reconstruction_contract=none"
    return json.dumps(contract, ensure_ascii=False)[:4000]
