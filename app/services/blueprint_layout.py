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
    canvas = (spec.get("structure") or {}).get("canvas") or {}
    requested = str(canvas.get("aspect_ratio") or "").strip()
    total_blocks = content_cards_count + footer_blocks_count + 2

    if requested in SUPPORTED_FORMATS:
        profile = dict(SUPPORTED_FORMATS[requested])
    elif content_cards_count <= 4:
        profile = dict(SUPPORTED_FORMATS["1:1"])
        requested = "1:1"
    elif content_cards_count <= 6:
        profile = dict(SUPPORTED_FORMATS["4:5"])
        requested = "4:5"
    elif total_blocks <= 12:
        profile = dict(SUPPORTED_FORMATS["3:4"])
        requested = "3:4"
    else:
        profile = dict(SUPPORTED_FORMATS["2:3"])
        requested = "2:3"

    # If the AI requested a square but the content is too dense, override safely.
    if requested == "1:1" and total_blocks > 9:
        profile = dict(SUPPORTED_FORMATS["3:4"])
        requested = "3:4"
    if requested in {"4:5", "3:4"} and total_blocks > 14:
        profile = dict(SUPPORTED_FORMATS["2:3"])
        requested = "2:3"

    size_text = str(canvas.get("recommended_size") or "")
    if "x" in size_text.lower():
        left, right = size_text.lower().split("x", 1)
        w = _as_int(left, profile["w"])
        h = _as_int(right, profile["h"])
        # Only accept sane portrait/social sizes.
        if 900 <= w <= 1600 and 900 <= h <= 2200 and h >= w:
            profile["w"] = w
            profile["h"] = h

    profile["aspect_ratio"] = requested
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
    x = max(0, min(W - 1, int(x)))
    y = max(0, min(H - 1, int(y)))
    w = max(1, min(W - x, int(w)))
    h = max(1, min(H - y, int(h)))
    if w < 80 or h < 80:
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
        if layout["x"] + layout["w"] > W or layout["y"] + layout["h"] > H:
            issues.append(f"block_{i+1}_outside_canvas")
            return False, [], issues
        layouts.append(layout)
    for i in range(len(layouts)):
        for j in range(i + 1, len(layouts)):
            ov = _overlap(layouts[i], layouts[j])
            if ov > min(layouts[i]["w"] * layouts[i]["h"], layouts[j]["w"] * layouts[j]["h"]) * 0.08:
                issues.append(f"blocks_{i+1}_{j+1}_overlap")
                return False, [], issues
    return True, layouts, issues


def auto_plan_layout(
    W: int,
    H: int,
    content_cards: list[dict[str, Any]],
    footer_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    margin = 30
    gap = 16
    n = len(content_cards)
    footer_count = min(len(footer_blocks), 3)

    header_h = 190 if H <= 1600 else 210
    footer_h = 0
    if footer_count == 1:
        footer_h = 190
    elif footer_count == 2:
        footer_h = 235
    elif footer_count >= 3:
        footer_h = 350 if H >= 1700 else 300

    available_h = H - header_h - footer_h - margin * 2
    if n <= 4:
        cols = 2
    else:
        cols = 3
    rows = max(1, math.ceil(n / cols))
    card_w = (W - 2 * margin - (cols - 1) * gap) // cols
    card_h = max(190, (available_h - (rows - 1) * gap) // rows)

    # If cards became too compressed, switch to taller plan semantics by letting footer shrink a bit.
    min_card_h = 235 if cols == 3 else 270
    if card_h < min_card_h and footer_h > 220:
        footer_h -= 80
        available_h = H - header_h - footer_h - margin * 2
        card_h = max(190, (available_h - (rows - 1) * gap) // rows)

    layouts: dict[str, Any] = {
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

    fy = start_y + rows * card_h + (rows - 1) * gap + 24
    if footer_count == 1:
        layouts["footer"].append({"x": margin, "y": fy, "w": W - 2 * margin, "h": min(170, H - fy - margin)})
    elif footer_count == 2:
        bw = (W - 2 * margin - gap) // 2
        fh = min(210, H - fy - margin)
        layouts["footer"].append({"x": margin, "y": fy, "w": bw, "h": fh})
        layouts["footer"].append({"x": margin + bw + gap, "y": fy, "w": bw, "h": fh})
    elif footer_count >= 3:
        bw = (W - 2 * margin - 2 * gap) // 3
        fh = min(270 if H >= 1700 else 230, H - fy - margin)
        for i in range(3):
            layouts["footer"].append({"x": margin + i * (bw + gap), "y": fy, "w": bw, "h": fh})
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
    for i, block in enumerate(content_cards, 1):
        policy = str(block.get("source_policy") or "").lower()
        if policy in {"preserve_from_reference", "use_reference_and_clean"} and not block.get("source_bbox"):
            issues.append(f"card_{i}_needs_source_bbox")
        if not str(block.get("title") or "").strip():
            issues.append(f"card_{i}_missing_title")
    return issues
