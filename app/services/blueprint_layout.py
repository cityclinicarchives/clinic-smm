import math
from typing import Any

SUPPORTED_FORMATS = {
    "1:1": {"w": 1200, "h": 1200, "name": "square"},
    "4:5": {"w": 1200, "h": 1500, "name": "instagram_feed"},
    "3:4": {"w": 1200, "h": 1600, "name": "telegram_pinterest"},
    "2:3": {"w": 1200, "h": 1800, "name": "tall_infographic"},
    "9:16": {"w": 1080, "h": 1920, "name": "story_reels"},
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def choose_format(spec: dict[str, Any], content_cards_count: int, footer_blocks_count: int) -> dict[str, Any]:
    """Choose safe master canvas for infographics.

    v26 rule: square is allowed only for simple cards. Complex infographics escalate
    to portrait formats automatically even if the AI requested 1:1.
    """
    canvas = (spec.get("structure") or {}).get("canvas") or {}
    requested = str(canvas.get("aspect_ratio") or "").strip()
    total_blocks = content_cards_count + footer_blocks_count + 2  # header + disclaimer/CTA area

    # Density-based default. 4:5 is the primary social feed master; taller formats for dense cards.
    if content_cards_count <= 4 and total_blocks <= 7:
        chosen = "1:1"
    elif content_cards_count <= 6 and total_blocks <= 10:
        chosen = "4:5"
    elif content_cards_count <= 9 and total_blocks <= 15:
        chosen = "3:4"
    else:
        chosen = "2:3"

    # Respect AI only if it is not too small for the actual content.
    if requested in SUPPORTED_FORMATS:
        order = ["1:1", "4:5", "3:4", "2:3", "9:16"]
        if order.index(requested) > order.index(chosen):
            chosen = requested
        # Never accept square for a dense infographic.
        if requested == "1:1" and (content_cards_count > 4 or total_blocks > 7):
            chosen = "4:5" if content_cards_count <= 6 else "3:4"

    profile = dict(SUPPORTED_FORMATS[chosen])

    # Accept recommended size only if it is portrait enough for the content density.
    size_text = str(canvas.get("recommended_size") or "")
    if "x" in size_text.lower():
        left, right = size_text.lower().split("x", 1)
        w = _as_int(left, profile["w"])
        h = _as_int(right, profile["h"])
        min_ratio = 1.0 if chosen == "1:1" else 1.20
        if 900 <= w <= 1600 and 900 <= h <= 2400 and h / max(w, 1) >= min_ratio:
            profile["w"] = w
            profile["h"] = h

    profile["aspect_ratio"] = chosen
    profile["total_blocks"] = total_blocks
    return profile


def _norm_layout(layout: Any, W: int, H: int) -> dict[str, int] | None:
    if not isinstance(layout, dict):
        return None
    try:
        x = float(layout.get("x", 0)); y = float(layout.get("y", 0))
        w = float(layout.get("w", layout.get("width", 0)))
        h = float(layout.get("h", layout.get("height", 0)))
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    if max(abs(x), abs(y), abs(w), abs(h)) <= 1.5:
        x, y, w, h = x * W, y * H, w * W, h * H
    x = int(round(x)); y = int(round(y)); w = int(round(w)); h = int(round(h))
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return None
    if w < 120 or h < 100:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _overlap(a: dict[str, int], b: dict[str, int]) -> int:
    ax2 = a["x"] + a["w"]; ay2 = a["y"] + a["h"]
    bx2 = b["x"] + b["w"]; by2 = b["y"] + b["h"]
    x = max(0, min(ax2, bx2) - max(a["x"], b["x"]))
    y = max(0, min(ay2, by2) - max(a["y"], b["y"]))
    return x * y


def validate_ai_layout(blocks: list[dict[str, Any]], W: int, H: int) -> tuple[bool, list[dict[str, int]], list[str]]:
    layouts: list[dict[str, int]] = []
    issues: list[str] = []
    for i, block in enumerate(blocks):
        layout = _norm_layout(block.get("layout"), W, H)
        if not layout:
            issues.append(f"block_{i+1}_missing_or_invalid_layout")
            return False, [], issues
        layouts.append(layout)
    for i in range(len(layouts)):
        for j in range(i + 1, len(layouts)):
            ov = _overlap(layouts[i], layouts[j])
            if ov > min(layouts[i]["w"] * layouts[i]["h"], layouts[j]["w"] * layouts[j]["h"]) * 0.05:
                issues.append(f"blocks_{i+1}_{j+1}_overlap")
                return False, [], issues
    return True, layouts, issues


def required_canvas_height(W: int, n_cards: int, n_footers: int, min_card_h: int = 300) -> int:
    margin = 34
    gap = 18
    header_h = 205
    cols = 2 if n_cards <= 4 else 3
    rows = max(1, math.ceil(n_cards / cols))
    footer_rows = 0 if n_footers <= 0 else (1 if n_footers <= 3 else math.ceil(n_footers / 3))
    footer_h = 0 if footer_rows == 0 else footer_rows * 210 + (footer_rows - 1) * gap + 30
    return header_h + margin * 2 + rows * min_card_h + (rows - 1) * gap + footer_h


def auto_plan_layout(
    W: int,
    H: int,
    content_cards: list[dict[str, Any]],
    footer_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic safe layout.

    v26: never silently squeezes or drops content. If the requested canvas is too
    short, the plan expands H up to 2400 rather than shrinking cards below readable size.
    """
    margin = 34
    gap = 18
    n = len(content_cards)
    footer_count = min(len(footer_blocks), 4)

    header_h = 205
    cols = 2 if n <= 4 else 3
    rows = max(1, math.ceil(n / cols))
    min_card_h = 310 if cols == 3 else 350

    needed_h = required_canvas_height(W, n, footer_count, min_card_h=min_card_h)
    if H < needed_h:
        H = min(max(H, needed_h), 2400)

    # Allocate card area first, then footers. If still tight, keep card height readable.
    footer_rows = 0 if footer_count == 0 else (1 if footer_count <= 3 else 2)
    footer_h_each = 210
    footer_total_h = 0 if footer_rows == 0 else footer_rows * footer_h_each + (footer_rows - 1) * gap + 30
    available_h = max(min_card_h * rows + (rows - 1) * gap, H - header_h - footer_total_h - margin * 2)
    card_w = (W - 2 * margin - (cols - 1) * gap) // cols
    card_h = max(min_card_h, (available_h - (rows - 1) * gap) // rows)

    layouts: dict[str, Any] = {
        "canvas": {"w": W, "h": H},
        "header": {"x": margin, "y": 24, "w": W - 2 * margin, "h": header_h - 34},
        "cards": [],
        "footer": [],
        "cols": cols,
        "rows": rows,
        "margin": margin,
        "gap": gap,
        "card_w": card_w,
        "card_h": card_h,
    }
    start_y = header_h
    for idx in range(n):
        row = idx // cols
        col = idx % cols
        x = margin + col * (card_w + gap)
        y = start_y + row * (card_h + gap)
        layouts["cards"].append({"x": x, "y": y, "w": card_w, "h": card_h})

    fy = start_y + rows * card_h + (rows - 1) * gap + 28
    if footer_count:
        # Footer blocks in up to 3 columns, then next row.
        fcols = min(3, footer_count)
        fw = (W - 2 * margin - (fcols - 1) * gap) // fcols
        for idx in range(footer_count):
            row = idx // fcols
            col = idx % fcols
            layouts["footer"].append({
                "x": margin + col * (fw + gap),
                "y": fy + row * (footer_h_each + gap),
                "w": fw,
                "h": footer_h_each,
            })
    return layouts


def validate_blueprint(spec: dict[str, Any], content_cards: list[dict[str, Any]], footer_blocks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    structure = spec.get("structure") or {}
    expected = structure.get("expected_block_count")
    try:
        expected_int = int(expected)
    except Exception:
        expected_int = None
    actual = len(structure.get("blocks") or [])
    if expected_int and expected_int != actual:
        issues.append(f"expected_block_count={expected_int}, actual_structure_blocks={actual}")
    if not content_cards:
        issues.append("no_content_cards")
    if len(content_cards) >= 7 and not footer_blocks:
        issues.append("dense_infographic_without_footer_blocks")
    for i, block in enumerate(content_cards, 1):
        policy = str(block.get("source_policy") or "").lower()
        if policy in {"preserve_from_reference", "use_reference_and_clean"} and not block.get("source_bbox"):
            issues.append(f"card_{i}_needs_source_bbox")
        if not str(block.get("title") or "").strip():
            issues.append(f"card_{i}_missing_title")
    return issues


def final_qa(plan: dict[str, Any], cards: list[dict[str, Any]], footer_blocks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    card_layouts = plan.get("cards") or []
    footer_layouts = plan.get("footer") or []
    if len(card_layouts) < len(cards):
        issues.append(f"rendered_cards={len(card_layouts)}, expected_cards={len(cards)}")
    if len(footer_layouts) < min(len(footer_blocks), 4):
        issues.append(f"rendered_footer_blocks={len(footer_layouts)}, expected_footer_blocks={min(len(footer_blocks), 4)}")
    W = (plan.get("canvas") or {}).get("w")
    H = (plan.get("canvas") or {}).get("h")
    if W and H:
        for name, layouts in [("card", card_layouts), ("footer", footer_layouts)]:
            for i, l in enumerate(layouts, 1):
                if l["x"] < 0 or l["y"] < 0 or l["x"] + l["w"] > W or l["y"] + l["h"] > H:
                    issues.append(f"{name}_{i}_outside_canvas")
    return issues
