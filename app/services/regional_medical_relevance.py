"""Rule-light regional/medical relevance helpers.

This module deliberately avoids hardcoding one replacement case. It only adds
validation pressure: every source unit must have an explicit decision and low
regional relevance cannot silently become a final keep card.
"""
from __future__ import annotations

from typing import Any


def validate_relevance_decisions(decisions: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not isinstance(decisions, dict):
        return ["regional_relevance_decisions_invalid"]
    source_decisions = decisions.get("source_unit_decisions")
    if not isinstance(source_decisions, list) or not source_decisions:
        return ["regional_relevance_no_source_decisions"]
    for idx, d in enumerate(source_decisions, start=1):
        if not isinstance(d, dict):
            continue
        title = d.get("source_title") or d.get("translated_title") or idx
        decision = str(d.get("decision") or "").lower()
        regional = str(d.get("regional_relevance") or "").lower()
        medical = str(d.get("medical_relevance") or "").lower()
        if decision not in {"keep", "merge", "replace", "remove"}:
            issues.append(f"regional_relevance_missing_decision:{title}")
        if regional == "low" and decision == "keep":
            issues.append(f"regional_relevance_low_but_kept:{title}")
        if medical == "low" and decision == "keep":
            issues.append(f"medical_relevance_low_but_kept:{title}")
        if decision in {"replace", "merge"} and not (d.get("final_unit") or d.get("final_unit_id") or d.get("replacement_title") or d.get("final_title")):
            issues.append(f"regional_relevance_decision_missing_target:{title}")
    return issues
