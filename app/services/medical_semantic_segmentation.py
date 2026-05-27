"""Medical semantic segmentation helpers for visual decomposition.

This module does not perform computer-vision segmentation by itself. It provides
strict medical-role validation and normalization for AI component maps before the
crop/assemble engine trusts them.

The goal is to force every medical comparison item to preserve the PRIMARY
medical evidence (bite, rash, lesion, inflammation, skin reaction) separately
from SECONDARY context objects (insect, icon, tool, arrow, decorative object).
"""

from __future__ import annotations

from typing import Any

PRIMARY_MEDICAL_TYPES = {
    "bite_photo",
    "bite_area",
    "skin_reaction",
    "lesion_area",
    "rash_photo",
    "symptom_photo",
    "wound_photo",
    "inflammation_area",
    "primary_medical_visual",
}

SECONDARY_CONTEXT_TYPES = {
    "insect_icon",
    "arthropod_icon",
    "context_object",
    "medical_icon",
    "object_photo",
    "tool_icon",
    "decorative_object",
}

REMOVE_TYPES = {
    "text_label",
    "caption",
    "background",
    "watermark",
    "social_ui",
    "username",
    "branding",
    "decorative",
}


def _stype(value: Any) -> str:
    return str(value or "").strip().lower()


def component_role(component: dict[str, Any]) -> str:
    """Return primary/secondary/remove/unknown semantic role."""
    ctype = _stype(component.get("type"))
    role = _stype(component.get("semantic_role") or component.get("role") or component.get("priority"))
    if role in {"primary", "primary_medical", "primary_medical_object"}:
        return "primary"
    if role in {"secondary", "secondary_context", "context"}:
        return "secondary"
    if ctype in PRIMARY_MEDICAL_TYPES:
        return "primary"
    if ctype in SECONDARY_CONTEXT_TYPES:
        return "secondary"
    if ctype in REMOVE_TYPES:
        return "remove"
    return "unknown"


def normalize_medical_component(component: dict[str, Any]) -> dict[str, Any]:
    out = dict(component)
    role = component_role(out)
    if role == "primary":
        out.setdefault("semantic_role", "primary_medical_object")
        out.setdefault("priority", "primary")
        out.setdefault("action", "preserve")
    elif role == "secondary":
        out.setdefault("semantic_role", "secondary_context_object")
        out.setdefault("priority", "secondary")
        out.setdefault("action", "preserve")
    elif role == "remove":
        out.setdefault("action", "remove")
    return out


def normalize_component_map(component_map: dict[str, Any]) -> dict[str, Any]:
    """Add semantic role defaults to AI component maps."""
    if not isinstance(component_map, dict):
        return {}
    out = dict(component_map)
    units = out.get("units")
    if not isinstance(units, list):
        return out
    new_units: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        u = dict(unit)
        comps = u.get("components")
        if isinstance(comps, list):
            u["components"] = [normalize_medical_component(c) if isinstance(c, dict) else c for c in comps]
        new_units.append(u)
    out["units"] = new_units
    return out


def medical_semantic_issues_for_unit(unit: dict[str, Any]) -> list[str]:
    """Validate one comparison unit for primary/secondary separation."""
    issues: list[str] = []
    title = str(unit.get("title") or unit.get("unit_id") or "unit")
    role = _stype(unit.get("unit_role"))
    strategy = _stype(unit.get("extraction_strategy"))
    if strategy in {"generate_new", "replace_with_new"}:
        return issues
    if role not in {"comparison_item", "card", "visual_card", ""}:
        return issues

    comps = unit.get("components") if isinstance(unit.get("components"), list) else []
    preserved = [c for c in comps if isinstance(c, dict) and _stype(c.get("action")) == "preserve"]
    primary = [c for c in preserved if component_role(c) == "primary"]
    secondary = [c for c in preserved if component_role(c) == "secondary"]

    # For medical infographic comparison cards the primary medical object must survive.
    if not primary:
        issues.append(f"medical_semantic_missing_primary:{title}")

    # If a context object exists in source, it should be separate from the primary component.
    # We cannot always require secondary, but if AI mentions context object textually and does not preserve it, flag soft issue.
    context_text = " ".join(str(unit.get(k) or "") for k in ("source_title", "title", "notes")).lower()
    if any(w in context_text for w in ["комар", "мурав", "клещ", "клоп", "пчел", "блох", "паук", "оса", "mosquito", "ant", "tick", "bee", "flea", "spider", "wasp", "bedbug"]):
        if not secondary:
            issues.append(f"medical_semantic_missing_secondary_context:{title}")

    for comp in primary:
        bbox = comp.get("bbox")
        if not isinstance(bbox, dict):
            issues.append(f"medical_semantic_primary_without_bbox:{title}")
            continue
        try:
            w = float(bbox.get("w", 0)); h = float(bbox.get("h", 0))
            if w <= 0 or h <= 0:
                issues.append(f"medical_semantic_bad_primary_bbox:{title}")
            if w * h > 0.25:
                issues.append(f"medical_semantic_primary_bbox_too_large:{title}")
        except Exception:
            issues.append(f"medical_semantic_bad_primary_bbox:{title}")

    # Merged visual cluster is a common failure: one bbox claims to include both bite and insect.
    for comp in preserved:
        keep = " ".join(str(x).lower() for x in (comp.get("keep") or []))
        ctype = _stype(comp.get("type"))
        if component_role(comp) == "primary" and any(w in keep for w in ["insect", "насеком", "комар", "мурав", "клещ", "пчел", "оса", "паук"]):
            issues.append(f"medical_semantic_primary_secondary_merged:{title}")
        if ctype in {"visual_cluster", "whole_card", "card_image"}:
            issues.append(f"medical_semantic_whole_cluster_not_allowed:{title}")
    return issues


def medical_semantic_issues(component_map: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    units = component_map.get("units") if isinstance(component_map, dict) else []
    if not isinstance(units, list):
        return ["medical_semantic_no_units"]
    for unit in units:
        if isinstance(unit, dict):
            issues.extend(medical_semantic_issues_for_unit(unit))
    return issues


def component_sort_key(component: dict[str, Any]) -> tuple[int, str]:
    """Stable order: primary first, secondary second."""
    role = component_role(component)
    rank = 0 if role == "primary" else 1 if role == "secondary" else 2
    return rank, str(component.get("component_id") or component.get("type") or "")
