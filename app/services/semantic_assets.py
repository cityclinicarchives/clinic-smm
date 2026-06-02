from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any
from collections import deque

from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.database import SessionLocal
from app.models import ContentAsset
from app.services.telegram_bot import download_file_bytes
from app.services.cost_tracker import (
    aggregate_costs,
    cost_for_image_generation,
    free_operation,
    save_cost_event,
)
from app.services.semantic_analysis_store import load_latest_analysis_document
from app.services.semantic_asset_store import register_artifact, list_artifacts, ensure_artifact_local, delete_artifacts, artifact_file_exists, purge_artifact_row


class SemanticAssetError(RuntimeError):
    pass


def _client():
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise SemanticAssetError("OPENAI_API_KEY не задан.")
    from openai import OpenAI
    return OpenAI(api_key=settings.openai_api_key)


def _safe_name(value: str, fallback: str = "asset") -> str:
    value = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9_-]+", "-", (value or "").strip()).strip("-")
    return value[:80] or fallback




def _source_image_for_asset(asset_id: int) -> Image.Image | None:
    """Load original Telegram image for native crop extraction.

    This keeps extract_from_source PNGs from being unnecessarily regenerated or
    upscaled. If source loading fails, caller falls back to image generation.
    """
    db = SessionLocal()
    try:
        asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
        if not asset or not asset.media_file_id:
            return None
        if (asset.media_type or '').lower() not in {'photo', 'image', 'document'}:
            return None
        data = download_file_bytes(asset.media_file_id)
        from io import BytesIO
        return Image.open(BytesIO(data)).convert('RGBA')
    except Exception:
        return None
    finally:
        db.close()


def _normalized_crop_box(task: dict[str, Any], image: Image.Image, expansion_ratio: float | None = None) -> tuple[int, int, int, int] | None:
    hint = task.get('source_crop_hint') if isinstance(task.get('source_crop_hint'), dict) else {}
    box = hint.get('relative_box')
    if not (isinstance(box, list) and len(box) == 4):
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in box]
    except Exception:
        return None
    # Clamp and validate.
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(1, x2), min(1, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    w, h = image.size
    left, top, right, bottom = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
    if right - left < 20 or bottom - top < 20:
        return None
    return _expand_crop_box((left, top, right, bottom), image.size, task, expansion_ratio=expansion_ratio)


def _expand_crop_box(box: tuple[int, int, int, int], image_size: tuple[int, int], task: dict[str, Any], expansion_ratio: float | None = None) -> tuple[int, int, int, int]:
    """Add a small safety margin so circle contours and insect legs/wings are not cut.

    The model is instructed to exclude old labels itself. This margin is deliberately
    modest: enough to save truncated outlines, but not enough to pull in captions in
    most layouts. The later component cleanup removes stray label fragments if they
    still enter the crop.
    """
    left, top, right, bottom = box
    img_w, img_h = image_size
    crop_w, crop_h = right - left, bottom - top
    hint = task.get('source_crop_hint') if isinstance(task.get('source_crop_hint'), dict) else {}
    try:
        confidence = float(hint.get('confidence', 0.0))
    except Exception:
        confidence = 0.0
    # v42.3: model crop boxes are often tight around thin insect wings/legs.
    # Use a larger default safety margin and let retry logic expand further if
    # the extracted foreground still touches a crop edge.
    if expansion_ratio is not None:
        ratio = max(0.0, float(expansion_ratio))
    else:
        ratio = 0.20
    if ratio <= 0:
        pad_x = 0
        pad_y = 0
    else:
        pad_x = max(8, int(crop_w * ratio))
        pad_y = max(8, int(crop_h * ratio))
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(img_w, right + pad_x),
        min(img_h, bottom + pad_y),
    )


def _color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> int:
    return abs(c1[0] - c2[0]) + abs(c1[1] - c2[1]) + abs(c1[2] - c2[2])



def _border_palette(img: Image.Image, max_colors: int = 4) -> tuple[list[tuple[int, int, int]], int]:
    """Return dominant border colors and adaptive tolerance for edge-aware background flood fill."""
    rgba = img.convert('RGBA')
    px = rgba.load()
    w, h = rgba.size
    samples: list[tuple[int, int, int]] = []
    step = max(1, min(w, h) // 80)
    for x in range(0, w, step):
        for y in (0, h - 1):
            r, g, b, a = px[x, y]
            if a > 10:
                samples.append((r, g, b))
    for y in range(0, h, step):
        for x in (0, w - 1):
            r, g, b, a = px[x, y]
            if a > 10:
                samples.append((r, g, b))
    if not samples:
        return [(255, 255, 255)], 60
    # Quantize colors so textured paper/cream backgrounds produce a compact palette.
    buckets: dict[tuple[int, int, int], int] = {}
    for r, g, b in samples:
        key = (round(r / 16) * 16, round(g / 16) * 16, round(b / 16) * 16)
        buckets[key] = buckets.get(key, 0) + 1
    palette = [k for k, _ in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:max_colors]]
    # Border variability: higher tolerance for textured/simple paper, capped to avoid eating skin.
    mean = tuple(sum(c[i] for c in samples) / len(samples) for i in range(3))
    avg_dev = sum(abs(c[0]-mean[0]) + abs(c[1]-mean[1]) + abs(c[2]-mean[2]) for c in samples) / max(1, len(samples))
    tolerance = int(max(52, min(96, 48 + avg_dev * 0.45)))
    return [(int(r), int(g), int(b)) for r, g, b in palette], tolerance


def _min_color_distance(color: tuple[int, int, int], palette: list[tuple[int, int, int]]) -> int:
    return min(_color_distance(color, bg) for bg in palette) if palette else 999


def _remove_edge_aware_background(crop: Image.Image) -> Image.Image:
    """Remove border-connected background with an edge-aware flood fill.

    Unlike a plain rectangular crop, this treats the crop border as background and
    removes only pixels reachable from the edge that are similar to the dominant
    border palette. Strong dark outlines, object shadows and high-contrast edges
    stop the flood fill. This is generic: it does not assume circles, embryos,
    insects, or any specific object shape.
    """
    img = crop.convert('RGBA')
    px = img.load()
    w, h = img.size
    if w < 3 or h < 3:
        return img
    palette, tolerance = _border_palette(img)
    q: deque[tuple[int, int]] = deque()
    seen: set[tuple[int, int]] = set()
    for x in range(w):
        q.append((x, 0)); q.append((x, h - 1))
    for y in range(h):
        q.append((0, y)); q.append((w - 1, y))

    def is_bg_like(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        if a <= 12:
            return True
        # Preserve dark outlines and text-like dark details for component cleanup;
        # do not let flood-fill cross them.
        if r + g + b < 210:
            return False
        return _min_color_distance((r, g, b), palette) <= tolerance

    while q:
        x, y = q.popleft()
        if (x, y) in seen or x < 0 or y < 0 or x >= w or y >= h:
            continue
        seen.add((x, y))
        if not is_bg_like(x, y):
            continue
        r, g, b, a = px[x, y]
        px[x, y] = (r, g, b, 0)
        for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen:
                q.append((nx, ny))
    return img


def _component_cleanup_and_trim(img: Image.Image, preserve_multiple_components: bool = False) -> Image.Image:
    """Remove detached text rows/artifacts and trim to the foreground object.

    v41 line-based text suppression.

    preserve_multiple_components=True is used for hero_visual objects: a hero can
    intentionally consist of several detached foreground parts, such as a nose
    plus a medication bottle. In that mode we still remove text-like fragments,
    but preserve multiple large non-text components instead of keeping only the
    largest connected component.

    Important safety rule: anything touching or very close to a large foreground
    component is preserved. This protects insect legs, antennae, wings, thin
    outlines and shadows. Text that physically touches the object is intentionally
    left for a later AI/refine fallback rather than risking damage to the object.
    """
    img = img.convert('RGBA')
    px = img.load()
    w, h = img.size

    def opaque(x: int, y: int) -> bool:
        return px[x, y][3] > 18

    def is_dark_pixel(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        if a <= 18:
            return False
        return (r + g + b) < 390

    seen: set[tuple[int, int]] = set()
    comps: list[dict[str, Any]] = []
    for sy in range(h):
        for sx in range(w):
            if (sx, sy) in seen or not opaque(sx, sy):
                continue
            q: deque[tuple[int, int]] = deque([(sx, sy)])
            seen.add((sx, sy))
            pts: list[tuple[int, int]] = []
            minx = maxx = sx
            miny = maxy = sy
            dark_count = 0
            very_dark_count = 0
            while q:
                x, y = q.popleft()
                pts.append((x, y))
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                r, g, b, a = px[x, y]
                if (r + g + b) < 390:
                    dark_count += 1
                if (r + g + b) < 270:
                    very_dark_count += 1
                for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and opaque(nx, ny):
                        seen.add((nx, ny))
                        q.append((nx, ny))
            area = len(pts)
            bw, bh = maxx - minx + 1, maxy - miny + 1
            density = area / max(1, bw * bh)
            comps.append({
                'pts': pts,
                'bbox': (minx, miny, maxx, maxy),
                'area': area,
                'bw': bw,
                'bh': bh,
                'density': density,
                'dark_ratio': dark_count / max(1, area),
                'very_dark_ratio': very_dark_count / max(1, area),
                'cx': (minx + maxx) / 2,
                'cy': (miny + maxy) / 2,
            })

    if not comps:
        return img

    comps.sort(key=lambda c: c['area'], reverse=True)
    total_area = sum(c['area'] for c in comps)

    def bbox_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        dx = max(0, max(bx1 - ax2, ax1 - bx2))
        dy = max(0, max(by1 - ay2, ay1 - by2))
        return max(dx, dy)

    def bbox_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int], pad: int = 0) -> bool:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        return not (ax2 + pad < bx1 or bx2 + pad < ax1 or ay2 + pad < by1 or by2 + pad < ay1)

    def union_bbox(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
        return (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )

    def looks_like_text_component(c: dict[str, Any]) -> bool:
        area, bw, bh = c['area'], c['bw'], c['bh']
        if area <= 0:
            return False
        dark = c['dark_ratio'] > 0.42 or c['very_dark_ratio'] > 0.18
        if not dark:
            return False
        # Letters/word fragments are usually compact dark components, relatively
        # small in height, often horizontally elongated, with sparse density.
        smallish = area <= max(2200, int(total_area * 0.09))
        short = bh <= max(34, int(h * 0.18))
        word_like = bw >= max(7, bh * 0.55) and c['density'] < 0.78
        punctuation_like = area <= max(120, int(total_area * 0.006)) and bh <= max(20, int(h * 0.10))
        line_like = (bw <= max(7, int(w * 0.025)) and bh >= bw * 3) or (bh <= max(7, int(h * 0.025)) and bw >= bh * 3)
        return smallish and short and (word_like or punctuation_like or line_like)

    # 1) HARD keep-main-connected-object mode.
    #
    # Previous versions used a soft rule: keep the largest component plus nearby
    # components. That protected insect legs well, but it also preserved detached
    # caption remnants ("P", "0"), dots above labels and thin vertical strokes.
    #
    # This strict mode keeps the largest foreground component and only components
    # that physically touch it, or overlap its bbox with a 1px tolerance. This is
    # intentionally conservative about detached pieces: if an element does not
    # touch the main object, it is deleted. In current bite/sting source cards the
    # insect is visually attached to the skin circle, so this removes old captions
    # while preserving legs/wings that are part of the connected object.
    #
    # Future exception path: for layouts where one semantic PNG intentionally
    # consists of multiple detached objects, the analyzer should emit an explicit
    # expected_foreground_components > 1 / multi_component extraction mode and we
    # can switch to a less strict extractor for that item.
    protected_ids: set[int] = {id(comps[0])}

    if preserve_multiple_components:
        # Hero visuals may contain several separate useful objects. Preserve any
        # sizeable non-text component inside the crop, while still allowing the
        # later text-row detector to delete small labels, dots and UI fragments.
        # This is intentionally generic: it does not know what the objects are
        # (nose/bottle/stomach/etc.), only that several large visual components
        # may belong to the same semantic hero.
        min_keep_area = max(180, int(total_area * 0.045))
        for c in comps[1:]:
            if c['area'] >= min_keep_area and not looks_like_text_component(c):
                protected_ids.add(id(c))

    changed = True
    touch_pad = 1
    while changed:
        changed = False
        protected_boxes = [c['bbox'] for c in comps if id(c) in protected_ids]
        for c in comps:
            if id(c) in protected_ids:
                continue
            touches = any(bbox_distance(pb, c['bbox']) <= touch_pad or bbox_overlap(pb, c['bbox'], pad=touch_pad) for pb in protected_boxes)
            if touches:
                protected_ids.add(id(c))
                changed = True

    protected_boxes = [c['bbox'] for c in comps if id(c) in protected_ids]
    protected_bbox = union_bbox(protected_boxes)

    # 3) Build text candidates: detached dark components that look like letters,
    # punctuation, word fragments or separator strokes.
    candidate_indices: list[int] = []
    for idx, c in enumerate(comps):
        if id(c) in protected_ids:
            continue
        dist = bbox_distance(protected_bbox, c['bbox'])
        detached = dist > max(2, int(min(w, h) * 0.008))
        if detached and looks_like_text_component(c):
            candidate_indices.append(idx)

    delete_ids: set[int] = set()

    # 4) Group candidates into horizontal text rows. This is the main fix for
    # remnants such as "Wasp / Yellow Jacket", "Common Ant" and top label dots.
    row_tol = max(6, int(h * 0.030))
    rows: list[list[int]] = []
    for idx in candidate_indices:
        cy = comps[idx]['cy']
        placed = False
        for row in rows:
            avg = sum(comps[i]['cy'] for i in row) / len(row)
            if abs(cy - avg) <= row_tol:
                row.append(idx)
                placed = True
                break
        if not placed:
            rows.append([idx])

    for row in rows:
        boxes = [comps[i]['bbox'] for i in row]
        rb = union_bbox(boxes)
        row_w, row_h = rb[2] - rb[0] + 1, rb[3] - rb[1] + 1
        row_area = sum(comps[i]['area'] for i in row)
        detached = bbox_distance(protected_bbox, rb) > max(2, int(min(w, h) * 0.008))
        # A text row can be many letter components or a single connected word.
        multi_letter_row = len(row) >= 2 and row_w > max(14, row_h * 1.25)
        single_word_row = len(row) == 1 and row_w > max(18, row_h * 1.35) and row_h <= max(30, int(h * 0.16))
        edge_or_label_zone = rb[1] < h * 0.30 or rb[3] > h * 0.70 or rb[0] < w * 0.25 or rb[2] > w * 0.75
        modest_size = row_area < max(3500, int(total_area * 0.16))
        not_touching_object = not any(bbox_overlap(pb, rb, pad=2) for pb in protected_boxes)
        if detached and not_touching_object and modest_size and (multi_letter_row or single_word_row or edge_or_label_zone):
            for i in row:
                delete_ids.add(id(comps[i]))

    # 5) Remove isolated detached specks/strokes. This catches tiny label remnants
    # after row grouping, while the protected-near-object rule saves antennae/legs.
    for c in comps:
        if id(c) in protected_ids or id(c) in delete_ids:
            continue
        dist = bbox_distance(protected_bbox, c['bbox'])
        if dist <= max(3, int(min(w, h) * 0.018)):
            continue
        very_small = c['area'] < max(75, int(total_area * 0.004))
        thin = ((c['bw'] <= max(6, int(w * 0.025)) and c['bh'] >= c['bw'] * 3) or
                (c['bh'] <= max(6, int(h * 0.025)) and c['bw'] >= c['bh'] * 3))
        dark = c['dark_ratio'] > 0.40 or c['very_dark_ratio'] > 0.16
        if dark and (very_small or thin):
            delete_ids.add(id(c))

    # 6) Delete every detached non-protected component. In hard mode, protection
    # is limited to the largest connected object cluster. This deliberately
    # removes all non-touching caption fragments, dots and strokes.
    for c in comps:
        if id(c) in protected_ids:
            continue
        # Delete all detached non-protected components. This includes text,
        # punctuation, random dots and thin strokes.
        delete_ids.add(id(c))

    # Apply deletion.
    for c in comps:
        if id(c) not in delete_ids:
            continue
        for x, y in c['pts']:
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, 0)

    # Trim to visible bbox with padding and center on a transparent square canvas.
    alpha = img.getchannel('A')
    bbox = alpha.getbbox()
    if not bbox:
        return img
    x1, y1, x2, y2 = bbox
    pad = max(8, int(max(x2 - x1, y2 - y1) * 0.08))
    x1, y1, x2, y2 = max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)
    trimmed = img.crop((x1, y1, x2, y2))
    tw, th = trimmed.size
    side = max(tw, th)
    canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(trimmed, ((side - tw) // 2, (side - th) // 2))
    return canvas



def _raw_foreground_alpha_from_border(crop: Image.Image) -> Image.Image:
    """Build a rough foreground mask on the raw crop before cleanup.

    This is intentionally generic and cheap: estimate background from crop
    borders, then mark pixels sufficiently different from that border palette as
    foreground. It is used only to decide whether the *raw* target touches crop
    edges. Cleanup must not be allowed to hide clipping.
    """
    img = crop.convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    if w < 3 or h < 3:
        return mask
    palette, tolerance = _border_palette(img)
    # Slightly stricter than flood-fill background removal: we want to detect
    # colored/dark target pixels near the border before cleanup eats them.
    threshold = max(34, min(82, int(tolerance * 0.72)))
    src = img.load()
    dst = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = src[x, y]
            if a <= 12:
                continue
            # Dark outlines/text and colored object pixels both count as raw
            # foreground. The largest-component step below suppresses most small
            # text fragments when deciding clipping.
            is_fg = (r + g + b < 690) or (_min_color_distance((r, g, b), palette) > threshold)
            if is_fg:
                dst[x, y] = 255
    return mask


def _component_touch_sides_from_alpha(alpha: Image.Image, preserve_multiple_components: bool = False, margin: int = 3) -> dict[str, bool]:
    """Return edge contacts for the main raw foreground component(s).

    Unlike _alpha_edge_touch_sides, this ignores tiny detached fragments and uses
    the main component bbox. This helps translate-first crop correction avoid
    accepting a clipped object simply because cleanup erased the clipped edge.
    """
    result = {"left": False, "right": False, "top": False, "bottom": False}
    try:
        a = alpha.convert("L")
        w, h = a.size
        if w <= 0 or h <= 0:
            return result
        px = a.load()
        seen: set[tuple[int, int]] = set()
        comps: list[dict[str, Any]] = []
        for sy in range(h):
            for sx in range(w):
                if (sx, sy) in seen or px[sx, sy] <= 16:
                    continue
                q: deque[tuple[int, int]] = deque([(sx, sy)])
                seen.add((sx, sy))
                area = 0
                minx = maxx = sx
                miny = maxy = sy
                while q:
                    x, y = q.popleft()
                    area += 1
                    minx, maxx = min(minx, x), max(maxx, x)
                    miny, maxy = min(miny, y), max(maxy, y)
                    for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
                        if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and px[nx, ny] > 16:
                            seen.add((nx, ny))
                            q.append((nx, ny))
                if area >= max(12, int(w * h * 0.0012)):
                    comps.append({"area": area, "bbox": (minx, miny, maxx, maxy)})
        if not comps:
            return result
        comps.sort(key=lambda c: c["area"], reverse=True)
        keep = [comps[0]]
        if preserve_multiple_components:
            min_keep = max(30, int(comps[0]["area"] * 0.18))
            keep.extend(c for c in comps[1:] if int(c["area"]) >= min_keep)
        x1 = min(c["bbox"][0] for c in keep)
        y1 = min(c["bbox"][1] for c in keep)
        x2 = max(c["bbox"][2] for c in keep)
        y2 = max(c["bbox"][3] for c in keep)
        m = max(1, min(int(margin), max(1, min(w, h) // 10)))
        result["left"] = x1 <= m
        result["right"] = x2 >= w - 1 - m
        result["top"] = y1 <= m
        result["bottom"] = y2 >= h - 1 - m
    except Exception:
        pass
    return result


def _raw_crop_touch_sides(crop: Image.Image, preserve_multiple_components: bool = False) -> dict[str, bool]:
    return _component_touch_sides_from_alpha(
        _raw_foreground_alpha_from_border(crop),
        preserve_multiple_components=preserve_multiple_components,
        margin=4,
    )


def _has_opposite_edge_touch(touch: dict[str, bool]) -> bool:
    return bool((touch.get("left") and touch.get("right")) or (touch.get("top") and touch.get("bottom")))

def _remove_flat_corner_background(crop: Image.Image) -> Image.Image:
    """Backward-compatible wrapper for the new edge-aware extractor."""
    return _remove_edge_aware_background(crop)


def _remove_text_like_edge_fragments(img: Image.Image, preserve_multiple_components: bool = False) -> Image.Image:
    """Backward-compatible wrapper for component cleanup and trimming."""
    return _component_cleanup_and_trim(img, preserve_multiple_components=preserve_multiple_components)


def _alpha_touches_edge(img: Image.Image, margin: int = 3, min_opaque: int = 8) -> bool:
    """Return True if visible foreground reaches the crop edge.

    This is a cheap clipping signal. If foreground touches the original crop edge,
    a wing/leg/outline may have been cut by a too-tight model bbox. The extractor
    will retry with a wider source crop before final trimming.
    """
    try:
        alpha = img.convert("RGBA").getchannel("A")
        w, h = alpha.size
        if w <= 0 or h <= 0:
            return False
        m = max(1, min(int(margin), max(1, min(w, h) // 10)))
        edge_boxes = [
            (0, 0, w, m),
            (0, max(0, h - m), w, h),
            (0, 0, m, h),
            (max(0, w - m), 0, w, h),
        ]
        for box in edge_boxes:
            crop = alpha.crop(box)
            if sum(1 for v in crop.getdata() if v > 16) >= min_opaque:
                return True
        return False
    except Exception:
        return False



def _alpha_edge_touch_sides(img: Image.Image, margin: int = 3, min_opaque: int = 8) -> dict[str, bool]:
    """Return which crop edges are touched by visible foreground.

    This is used by translate-first extraction. If a foreground object touches
    the left edge, the crop is likely shifted too far right or too tight on the
    left side. We first move the crop window instead of expanding it, because
    expansion can pull in neighboring infographic objects when source items are
    close to each other.
    """
    result = {"left": False, "right": False, "top": False, "bottom": False}
    try:
        alpha = img.convert("RGBA").getchannel("A")
        w, h = alpha.size
        if w <= 0 or h <= 0:
            return result
        m = max(1, min(int(margin), max(1, min(w, h) // 10)))
        boxes = {
            "top": (0, 0, w, m),
            "bottom": (0, max(0, h - m), w, h),
            "left": (0, 0, m, h),
            "right": (max(0, w - m), 0, w, h),
        }
        for side, box in boxes.items():
            crop = alpha.crop(box)
            result[side] = sum(1 for v in crop.getdata() if v > 16) >= min_opaque
    except Exception:
        pass
    return result


def _shift_crop_box_by_touch(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    touch: dict[str, bool],
    step_ratio: float = 0.05,
) -> tuple[int, int, int, int]:
    """Move a crop window toward clipped foreground without changing its size."""
    left, top, right, bottom = box
    img_w, img_h = image_size
    bw, bh = right - left, bottom - top
    if bw <= 0 or bh <= 0:
        return box
    dx = 0
    dy = 0
    step_x = max(1, int(round(bw * step_ratio)))
    step_y = max(1, int(round(bh * step_ratio)))
    # If foreground touches an edge, move the window toward that edge to reveal
    # the clipped side. Do not expand until translation has failed.
    if touch.get("left") and not touch.get("right"):
        dx -= step_x
    elif touch.get("right") and not touch.get("left"):
        dx += step_x
    if touch.get("top") and not touch.get("bottom"):
        dy -= step_y
    elif touch.get("bottom") and not touch.get("top"):
        dy += step_y

    # If both opposite sides touch, shifting cannot solve that axis; leave it for
    # the later scale fallback.
    new_left = min(max(0, left + dx), max(0, img_w - bw))
    new_top = min(max(0, top + dy), max(0, img_h - bh))
    return (new_left, new_top, new_left + bw, new_top + bh)


def _box_can_expand(box: tuple[int, int, int, int], image_size: tuple[int, int], min_room: int = 2) -> bool:
    left, top, right, bottom = box
    w, h = image_size
    return left > min_room or top > min_room or right < w - min_room or bottom < h - min_room



def _foreground_quality_metrics(img: Image.Image) -> dict[str, Any]:
    """Cheap generic sanity metrics for extracted PNG quality.

    This does not try to identify the medical object. It only catches obviously
    suspicious crops: tiny foreground, very thin text-like strips, or nearly
    empty outputs. These signals are useful when analyzer bbox points to a
    neighboring object or crop cleanup has eaten the target object.
    """
    try:
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        w, h = alpha.size
        data = list(alpha.getdata())
        fg = sum(1 for v in data if v > 24)
        bbox = alpha.getbbox()
        if not bbox or w <= 0 or h <= 0:
            return {"fg_ratio": 0.0, "bbox_ratio": 0.0, "aspect": 0.0, "bbox": None, "score": 0.0}
        x1, y1, x2, y2 = bbox
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        fg_ratio = fg / float(max(1, w * h))
        bbox_ratio = (bw * bh) / float(max(1, w * h))
        aspect = max(bw / float(bh), bh / float(bw))
        # Larger is better, but heavily penalize text-like / tiny crops.
        score = fg_ratio * 2.0 + bbox_ratio * 0.7
        if aspect > 4.0:
            score *= 0.35
        if fg_ratio < 0.015:
            score *= 0.25
        return {
            "fg_ratio": round(fg_ratio, 4),
            "bbox_ratio": round(bbox_ratio, 4),
            "aspect": round(aspect, 2),
            "bbox": [int(x1), int(y1), int(x2), int(y2)],
            "score": round(score, 4),
        }
    except Exception:
        return {"fg_ratio": 0.0, "bbox_ratio": 0.0, "aspect": 0.0, "bbox": None, "score": 0.0}


def _is_suspicious_extraction(metrics: dict[str, Any], *, preserve_multiple_components: bool = False) -> tuple[bool, str]:
    """Return whether extracted PNG looks suspicious without semantic understanding."""
    fg = float(metrics.get("fg_ratio") or 0.0)
    aspect = float(metrics.get("aspect") or 0.0)
    bbox_ratio = float(metrics.get("bbox_ratio") or 0.0)
    if fg <= 0.002 or bbox_ratio <= 0.004:
        return True, "almost_empty_foreground"
    if fg < 0.012:
        return True, "tiny_foreground"
    # Very elongated foreground is usually a text line, separator, or sliced object.
    # Hero multi-component crops can be wider, so be less strict there.
    aspect_limit = 6.5 if preserve_multiple_components else 4.8
    if aspect > aspect_limit and bbox_ratio < 0.45:
        return True, "text_like_or_sliced_foreground"
    return False, ""

def _is_multi_component_extract_task(task: dict[str, Any]) -> bool:
    """Return True when one semantic PNG may intentionally contain several detached objects.

    The main current use case is universal hero_visual extraction. The analyzer
    may emit the role directly, or encode it through entity/png ids. We keep this
    heuristic small and generic to avoid topic-specific lists.
    """
    role = str(task.get("entity_role") or "").lower()
    entity_id = str(task.get("entity_id") or "").lower()
    png_id = str(task.get("png_id") or "").lower()
    mode = str(task.get("extraction_mode") or task.get("component_mode") or "").lower()
    if role == "hero_visual" or "hero" in entity_id or png_id.startswith("hero"):
        return True
    if mode in {"multi_component", "multi-component", "hero_visual"}:
        return True
    return False

def _extract_source_png(task: dict[str, Any], source_image: Image.Image | None, path: Path, diagnostics: list[dict[str, Any]] | None = None) -> bool:
    """Extract a Semantic PNG with raw-crop validation before cleanup.

    v43.5 crop validation pipeline:
    1) Start from analyzer bbox exactly as provided.
    2) Validate the RAW crop before any background cleanup.
    3) Translate-first in repeated 5% steps until the raw main object no longer
       touches crop edges.
    4) Translation fails when the object starts touching opposite edges on an
       axis, or when the crop window cannot move further.
    5) Then scale fallback in repeated 5% steps (5→10→15→20→25).
    6) Only after raw crop is accepted do background cleanup and artifact removal.
    """
    if source_image is None:
        return False

    preserve_multi = _is_multi_component_extract_task(task)
    png_id = str(task.get("png_id") or task.get("entity_id") or "semantic_png")
    local_diag: dict[str, Any] = {
        "png_id": png_id,
        "mode": "raw_validate_translate_first",
        "translate_attempts": 0,
        "shift_steps": [],
        "scale_fallback": "",
        "suspicious": False,
        "suspicious_reason": "",
        "final_box": [],
        "raw_touch": {},
        "cleanup_applied_after_crop_accept": False,
        "metrics": {},
    }

    def raw_touch_for_box(box: tuple[int, int, int, int]) -> dict[str, bool]:
        return _raw_crop_touch_sides(source_image.crop(box), preserve_multiple_components=preserve_multi)

    def cleanup_accepted_box(box: tuple[int, int, int, int]) -> tuple[Image.Image, dict[str, Any], bool, str]:
        crop = source_image.crop(box)
        if bool(task.get('transparent_background', True)):
            edge_clean = _remove_flat_corner_background(crop)
            cleaned = _remove_text_like_edge_fragments(edge_clean, preserve_multiple_components=preserve_multi)
        else:
            cleaned = crop.convert('RGBA')
        metrics = _foreground_quality_metrics(cleaned)
        suspicious, reason = _is_suspicious_extraction(metrics, preserve_multiple_components=preserve_multi)
        return cleaned, metrics, suspicious, reason

    base_box = _normalized_crop_box(task, source_image, expansion_ratio=0.0)
    if base_box is None:
        return False

    best: Image.Image | None = None
    best_score = -1.0
    best_diag: dict[str, Any] | None = None

    def remember_candidate(cleaned: Image.Image, metrics: dict[str, Any], box_used: tuple[int, int, int, int], suspicious: bool, reason: str, stage: str, raw_touch: dict[str, bool]) -> None:
        nonlocal best, best_score, best_diag
        score = float(metrics.get("score") or 0.0)
        if suspicious:
            score *= 0.55
        if any(raw_touch.values()):
            score *= 0.45
        if score > best_score:
            best = cleaned
            best_score = score
            best_diag = {
                "stage": stage,
                "box": [int(v) for v in box_used],
                "metrics": metrics,
                "suspicious": bool(suspicious),
                "suspicious_reason": reason,
                "raw_touch": dict(raw_touch),
            }

    def accept_box(box: tuple[int, int, int, int], stage: str, raw_touch: dict[str, bool], scale_label: str = "") -> bool:
        cleaned, metrics, suspicious, reason = cleanup_accepted_box(box)
        remember_candidate(cleaned, metrics, box, suspicious, reason, stage, raw_touch)
        if suspicious:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        cleaned.save(path, 'PNG')
        local_diag.update({
            "scale_fallback": scale_label,
            "final_box": [int(v) for v in box],
            "raw_touch": dict(raw_touch),
            "cleanup_applied_after_crop_accept": True,
            "metrics": metrics,
        })
        if diagnostics is not None:
            diagnostics.append(local_diag)
        return True

    # 1) Translate-first. Move in repeated 5% steps. Do not cleanup while
    # deciding whether the crop is spatially valid.
    box = base_box
    seen_boxes: set[tuple[int, int, int, int]] = set()
    for attempt in range(6):  # base + up to five 5% shifts = 25% cumulative path
        if box in seen_boxes:
            break
        seen_boxes.add(box)
        local_diag["translate_attempts"] += 1
        touch = raw_touch_for_box(box)
        local_diag["raw_touch"] = dict(touch)
        if not any(touch.values()):
            if accept_box(box, "translate", touch, ""):
                return True
            # If raw crop is spatially OK but cleanup quality is suspicious,
            # continue to small scale fallback rather than accept a bad output.
            break
        # Opposite edges mean the current crop does not fit the object on that
        # axis. Shifting further cannot solve it; switch to scale fallback.
        if _has_opposite_edge_touch(touch):
            local_diag["shift_steps"].append({"touch": touch, "stop_reason": "opposite_edges_touch", "box": [int(v) for v in box]})
            break
        shifted = _shift_crop_box_by_touch(box, source_image.size, touch, step_ratio=0.05)
        if shifted == box:
            local_diag["shift_steps"].append({"touch": touch, "stop_reason": "cannot_shift_further", "box": [int(v) for v in box]})
            break
        # If this shift makes the object touch the opposite edge, the crop is
        # too small. Do not keep shifting; scale fallback is the correct route.
        shifted_touch = raw_touch_for_box(shifted)
        local_diag["shift_steps"].append({"touch": touch, "from": [int(v) for v in box], "to": [int(v) for v in shifted], "after_touch": shifted_touch})
        if _has_opposite_edge_touch(shifted_touch):
            box = shifted
            break
        box = shifted

    # 2) Scale fallback. Start from the best translated box, then expand in 5%
    # increments. After every expansion, validate raw crop before cleanup.
    for ratio in (0.05, 0.10, 0.15, 0.20, 0.25):
        expanded = _expand_crop_box(box, source_image.size, task, expansion_ratio=ratio)
        touch = raw_touch_for_box(expanded)
        if any(touch.values()):
            # Still spatially clipped; do not cleanup/accept yet.
            local_diag.setdefault("scale_steps", []).append({"scale": f"{int(ratio*100)}%", "box": [int(v) for v in expanded], "raw_touch": touch, "accepted": False})
            continue
        if accept_box(expanded, f"scale_{ratio:.2f}", touch, f"{int(ratio*100)}%"):
            local_diag.setdefault("scale_steps", []).append({"scale": f"{int(ratio*100)}%", "box": [int(v) for v in expanded], "raw_touch": touch, "accepted": True})
            return True

    # 3) Debug failed crop. Do NOT save a best-effort crop as a successful PNG.
    # If translate + scale still cannot produce a raw crop where the main object
    # is fully inside the crop, save the last/best attempt only as a debug artifact
    # so the next test can show what the extractor actually did.
    debug_dir = path.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / f"{path.stem}_debug_last_attempt.png"

    debug_box = box
    debug_touch = raw_touch_for_box(debug_box)
    debug_stage = "debug_last_attempt"
    debug_metrics: dict[str, Any] = {}
    debug_reason = "still_touches_edge_after_translate_scale"

    # Prefer the best scored candidate for visual inspection if one exists, but
    # keep the artifact marked as debug/failed rather than success.
    if best is not None and best_diag:
        best.save(debug_path, 'PNG')
        debug_stage = str(best_diag.get("stage") or debug_stage)
        debug_box = tuple(int(v) for v in (best_diag.get("box") or debug_box))  # type: ignore[arg-type]
        debug_touch = dict(best_diag.get("raw_touch") or debug_touch)
        debug_metrics = best_diag.get("metrics") or {}
        debug_reason = str(best_diag.get("suspicious_reason") or debug_reason)
    else:
        cleaned, metrics, suspicious, reason = cleanup_accepted_box(debug_box)
        cleaned.save(debug_path, 'PNG')
        debug_metrics = metrics
        if reason:
            debug_reason = reason

    local_diag.update({
        "status": "debug_saved",
        "reason": "still_touches_edge_after_translate_scale",
        "debug_path": str(debug_path),
        "scale_fallback": debug_stage,
        "final_box": [int(v) for v in debug_box],
        "metrics": debug_metrics,
        "suspicious": True,
        "suspicious_reason": debug_reason,
        "raw_touch": debug_touch,
        "cleanup_applied_after_crop_accept": False,
    })
    if diagnostics is not None:
        diagnostics.append(local_diag)
    return False

def _infer_grid_columns(payload: dict[str, Any]) -> int:
    """Infer Composer grid columns for dynamic semantic PNG sizing.

    Prefer explicit layout text like 3×3 / 4x5. If absent, infer from card
    count using common social-infographic grids. This is intentionally simple
    and cheap; it avoids adding prompt/token overhead.
    """
    bp = payload.get("design_blueprint") if isinstance(payload.get("design_blueprint"), dict) else {}
    layout = str(bp.get("layout") or "")
    # Match 3×3, 4x5, 2 колонки × 5 рядов, etc.
    m = re.search(r"(\d+)\s*[×xXхХ]\s*(\d+)", layout)
    if m:
        try:
            return max(1, min(8, int(m.group(1))))
        except Exception:
            pass
    cards = bp.get("cards") if isinstance(bp.get("cards"), list) else []
    n = len(cards)
    if n <= 2:
        return max(1, n)
    if n in {3, 4}:
        return 2
    if n <= 9:
        return 3
    if n <= 20:
        return 4
    return 5


def _semantic_png_target_size(payload: dict[str, Any], task: dict[str, Any]) -> int:
    """Compute desired saved PNG side from final infographic grid.

    Formula agreed for v42: side = min(512, max(256, round(canvas_width / columns))).
    Explicit output_size {w,h} still works for old JSON; auto_from_layout uses formula.
    """
    size = task.get("output_size") if isinstance(task.get("output_size"), dict) else {}
    mode = str(size.get("mode") or "").lower()
    if mode != "auto_from_layout" and size.get("w") and size.get("h"):
        try:
            return max(128, min(512, int(min(int(size.get("w")), int(size.get("h"))))))
        except Exception:
            pass
    bp = payload.get("design_blueprint") if isinstance(payload.get("design_blueprint"), dict) else {}
    canvas = bp.get("canvas") if isinstance(bp.get("canvas"), dict) else {}
    try:
        canvas_w = int(canvas.get("width") or 1080)
    except Exception:
        canvas_w = 1080
    cols = _infer_grid_columns(payload)
    return int(min(512, max(256, round(canvas_w / max(1, cols)))))


def _openai_square_size_for_target(target_side: int) -> int:
    """Return the OpenAI Image API request side.

    Current image models do not support arbitrary small sizes like 256x256,
    270x270 or 512x512. Semantic PNG target sizing is still useful, but it
    must be applied *after* generation. Therefore every AI-generated semantic
    PNG is requested as 1024x1024 and then downscaled locally to target_side.
    """
    return 1024


def _resize_png_down_to_target(path: Path, target_side: int) -> None:
    """Downscale large generated/cropped PNGs to target_side; never upscale."""
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        max_side = max(w, h)
        if max_side <= target_side:
            return
        scale = target_side / float(max_side)
        new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        # Put on a square transparent canvas for stable Composer layout.
        canvas = Image.new("RGBA", (target_side, target_side), (0, 0, 0, 0))
        canvas.alpha_composite(img, ((target_side - new_size[0]) // 2, (target_side - new_size[1]) // 2))
        canvas.save(path, "PNG")
    except Exception:
        return

def analysis_dir() -> Path:
    path = Path("storage/analysis")
    path.mkdir(parents=True, exist_ok=True)
    return path


def semantic_png_dir(asset_id: int, state_id: int | None = None) -> Path:
    suffix = f"state-{state_id}" if state_id else "latest"
    path = Path("storage/semantic_png") / f"asset-{asset_id}" / suffix
    path.mkdir(parents=True, exist_ok=True)
    return path


def reconstruction_dir() -> Path:
    path = Path("storage/reconstructions")
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_analysis_file(asset_id: int) -> Path:
    files = sorted(
        analysis_dir().glob(f"asset-{asset_id}-state-*-semantic-analysis.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise SemanticAssetError(f"JSON анализа для исходника #{asset_id} не найден.")
    return files[0]


def load_analysis(asset_id: int) -> dict[str, Any]:
    # Prefer PostgreSQL because Railway local files can disappear after redeploy/restart.
    data = load_latest_analysis_document(asset_id)
    if data is not None:
        return data

    # Backward-compatible fallback for old deployments / local development.
    path = find_analysis_file(asset_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_analysis_path"] = str(path)
    return data




def _register_artifact_safe(asset_id: int, state_id: int | None, kind: str, path: Path) -> None:
    try:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            register_artifact(asset_id=asset_id, state_id=state_id, kind=kind, local_path=path, upload=True)
    except Exception:
        # R2/Postgres registry must never break the image pipeline.
        pass


def _matches_semantic_png_row(row: Any, output_name: str, local_path: Path) -> bool:
    wanted = {local_path.name, f"{output_name}.png"}
    file_name = str(getattr(row, "file_name", "") or "")
    return file_name in wanted or file_name.startswith(output_name)


def _cleanup_stale_semantic_png_rows(asset_id: int, state_id: int | None, output_name: str, local_path: Path) -> int:
    """Remove PostgreSQL cache rows whose R2/local file no longer exists.

    This fixes the case when files are deleted manually in Cloudflare R2 but
    PostgreSQL still says they exist. Returns how many stale rows were purged.
    """
    purged = 0
    try:
        rows = list_artifacts(asset_id, state_id, kind="semantic_png")
        for row in rows:
            if not _matches_semantic_png_row(row, output_name, local_path):
                continue
            try:
                if not artifact_file_exists(row):
                    purge_artifact_row(row, delete_local=True, delete_remote=False)
                    purged += 1
            except Exception:
                # If storage check itself fails, do not reuse this row. Remove only
                # the local copy so generation can retry on the next step.
                try:
                    if local_path.exists():
                        local_path.unlink()
                except Exception:
                    pass
                purged += 1
    except Exception:
        pass
    return purged


def _restore_semantic_png_from_registry(asset_id: int, state_id: int | None, output_name: str, local_path: Path) -> bool:
    """Restore Semantic PNG from PostgreSQL/R2 registry only if the physical file exists."""
    try:
        rows = list_artifacts(asset_id, state_id, kind="semantic_png")
        for row in rows:
            if not _matches_semantic_png_row(row, output_name, local_path):
                continue
            if not artifact_file_exists(row):
                purge_artifact_row(row, delete_local=True, delete_remote=False)
                continue
            restored = ensure_artifact_local(row, local_path)
            if restored and restored.exists() and restored.stat().st_size > 0:
                return True
    except Exception:
        return False
    return False


def _save_blueprint_artifact(asset_id: int, state_id: int | None, payload: dict[str, Any]) -> None:
    try:
        out_dir = Path("storage/blueprints") / f"asset-{asset_id}" / (f"state-{state_id}" if state_id else "latest")
        out_dir.mkdir(parents=True, exist_ok=True)
        blueprint = {
            "asset_id": asset_id,
            "project_state_id": state_id,
            "design_blueprint": payload.get("design_blueprint"),
            "content_pack": (payload.get("custom") or {}).get("content_pack") if isinstance(payload.get("custom"), dict) else None,
            "smart_blocks": (payload.get("custom") or {}).get("smart_blocks") if isinstance(payload.get("custom"), dict) else payload.get("smart_blocks"),
            "semantic_png_plan": payload.get("semantic_png_plan"),
        }
        path = out_dir / "blueprint.json"
        path.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        _register_artifact_safe(asset_id, state_id, "blueprint", path)
    except Exception:
        pass

def _payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise SemanticAssetError("В JSON анализа нет payload.")
    return payload


def _state_id(data: dict[str, Any]) -> int | None:
    try:
        return int(data.get("project_state_id"))
    except Exception:
        return None


def _content_pack(payload: dict[str, Any]) -> dict[str, Any]:
    custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
    pack = custom.get("content_pack") if isinstance(custom.get("content_pack"), dict) else {}
    if pack:
        return pack

    # Fallback for old JSON files.
    bp = payload.get("design_blueprint") if isinstance(payload.get("design_blueprint"), dict) else {}
    cards = []
    for card in bp.get("cards") or []:
        if isinstance(card, dict):
            cards.append({
                "card_id": card.get("card_id"),
                "png_id": card.get("png_id"),
                "title": card.get("title") or card.get("label") or "",
                "short_text": card.get("short_text") or "",
            })
    return {
        "header": bp.get("header") or {},
        "cards": cards,
        "footer_blocks": bp.get("footer_blocks") or [],
        "post": payload.get("post") or {},
    }


def _visual_template_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
    groups = custom.get("visual_template_groups") or payload.get("visual_template_groups")
    return groups if isinstance(groups, list) else []


def _task_style_lock_group(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = str(task.get("entity_id") or "")
    explicit_group = str(task.get("style_lock_group") or "")
    for group in _visual_template_groups(payload):
        if not isinstance(group, dict):
            continue
        if str(group.get("template_mode") or "").lower() != "shared":
            continue
        if explicit_group and str(group.get("group_id") or "") == explicit_group:
            return group
        members = [str(x) for x in (group.get("members") or [])]
        if entity_id and entity_id in members:
            return group
    return None


def _reference_png_for_group(group: dict[str, Any] | None, payload: dict[str, Any]) -> str | None:
    if not isinstance(group, dict):
        return None
    ref = group.get("reference_png_id")
    if ref:
        return str(ref)
    ref_entity = str(group.get("reference_entity_id") or "")
    for task in payload.get("semantic_png_plan") or []:
        if isinstance(task, dict) and str(task.get("entity_id") or "") == ref_entity and task.get("png_id"):
            return str(task.get("png_id"))
    return None




def _task_by_png_id(payload: dict[str, Any], png_id: str) -> dict[str, Any] | None:
    for task in payload.get("semantic_png_plan") or []:
        if isinstance(task, dict) and str(task.get("png_id") or "") == str(png_id):
            return task
    return None


def _reference_png_path_for_task(
    task: dict[str, Any],
    payload: dict[str, Any],
    asset_id: int,
    state_id: int | None,
    out_dir: Path,
    source_image: Image.Image | None,
) -> tuple[Path | None, str | None, str | None]:
    """Return a physical reference PNG for shared-template image generation.

    The analyzer may identify a visual_template_group where many cards share the
    same visual matrix. In that case, text-only style lock is not enough: this
    helper tries to provide the actual extracted/preserved reference PNG to the
    Image Edit endpoint. It never blocks the pipeline; if the reference cannot be
    found or extracted, caller falls back to normal text-to-image generation.
    """
    group = _task_style_lock_group(task, payload)
    if not group:
        return None, None, None
    ref_png_id = str(task.get("reference_png_id") or _reference_png_for_group(group, payload) or "").strip()
    if not ref_png_id:
        return None, str(group.get("group_id") or ""), None

    ref_task = _task_by_png_id(payload, ref_png_id)
    output_name = _safe_name(ref_task.get("output_name") or ref_png_id, ref_png_id) if isinstance(ref_task, dict) else _safe_name(ref_png_id)
    ref_path = out_dir / f"{output_name}.png"
    if ref_path.exists() and ref_path.stat().st_size > 0:
        return ref_path, str(group.get("group_id") or ""), ref_png_id

    # Try R2/PostgreSQL/local registry.
    try:
        if _restore_semantic_png_from_registry(asset_id, state_id, output_name, ref_path):
            return ref_path, str(group.get("group_id") or ""), ref_png_id
    except Exception:
        pass

    # If the chosen reference is an extractable source object and has not been
    # generated yet in this run, create it locally so it can act as the style ref.
    if isinstance(ref_task, dict) and str(ref_task.get("operation") or "").lower() == "extract_from_source":
        try:
            if _extract_source_png(ref_task, source_image, ref_path):
                target_side = _semantic_png_target_size(payload, ref_task)
                _resize_png_down_to_target(ref_path, target_side)
                _register_artifact_safe(asset_id, state_id, "semantic_png", ref_path)
                return ref_path, str(group.get("group_id") or ""), ref_png_id
        except Exception:
            return None, str(group.get("group_id") or ""), ref_png_id

    return None, str(group.get("group_id") or ""), ref_png_id


def _generate_image_with_reference(
    client: Any,
    prompt: str,
    ref_path: Path,
    api_side: int,
) -> Any:
    """Generate an image using a real reference PNG via the Image Edit endpoint.

    Official OpenAI Images Edit API accepts one or more input images for GPT Image
    models. We use the reference as a visual style/template guide, not as a mask.
    """
    kwargs: dict[str, Any] = {
        "model": settings.openai_image_model,
        "prompt": prompt,
        "size": f"{api_side}x{api_side}",
        "n": 1,
        "quality": getattr(settings, "openai_image_quality", "low"),
    }
    fidelity = str(getattr(settings, "openai_image_input_fidelity", "low") or "low").lower()
    if fidelity in {"low", "high"}:
        kwargs["input_fidelity"] = fidelity
    try:
        kwargs["background"] = "transparent"
    except Exception:
        pass
    with ref_path.open("rb") as image_file:
        return client.images.edit(image=image_file, **kwargs)

def _style_lock_text(task: dict[str, Any], payload: dict[str, Any]) -> str:
    group = _task_style_lock_group(task, payload)
    if not group:
        # Generic style rule for independent icons.
        return (
            "STYLE CONSISTENCY: keep the general infographic style, clean edges, "
            "matching palette, matching detail level, no text, no background."
        )
    invariants = ", ".join(str(x) for x in (group.get("invariant_features") or [])[:4])
    variables = ", ".join(str(x) for x in (group.get("variable_features") or [])[:4])
    ref = task.get("reference_png_id") or _reference_png_for_group(group, payload) or "best reference PNG in the group"
    similarity = group.get("similarity") or ""
    return f"""
STRICT SHARED TEMPLATE LOCK + SEMANTIC VARIATION:
- This item belongs to visual_template_group={group.get('group_id')} similarity={similarity}.
- Reference PNG/entity: {ref}. If a reference image is provided, use it as a STYLE/TEMPLATE reference, not as the medical content to copy.
- Preserve the shared visual template strongly: composition, object base geometry, scale, line thickness, palette, lighting, sharpness and detail level.
- Change the variable distinguishing feature strongly: the target symptom/detail/stage/secondary object must be visibly different from the reference.
- Keep invariant features: {invariants or 'composition, scale, palette, line/detail level'}.
- Change variable features: {variables or 'symptom/detail/stage/secondary object'}.
- Do not clone the reference reaction/detail. Do not reuse the same local pattern if the task requires a different condition/object.
- Do not redesign, do not invent a new style, do not make it blurrier, glossier, softer or more abstract than the reference.
- Preserve style = high; semantic variation = high.
""".strip()


def build_semantic_png_prompt(task: dict[str, Any], payload: dict[str, Any]) -> str:
    analysis_state = payload.get("analysis_state") if isinstance(payload.get("analysis_state"), dict) else {}
    topic = analysis_state.get("topic") or "медицинская инфографика"
    instruction = task.get("instruction_for_python_or_image_ai") or "Создать смысловой PNG-объект для инфографики."
    must_include = ", ".join(str(x) for x in task.get("must_include", [])[:8])
    must_exclude = ", ".join(str(x) for x in task.get("must_exclude", [])[:8])
    quality_strategy = task.get("quality_strategy") or "regenerate_high_detail"
    style_lock = _style_lock_text(task, payload)
    return f"""
Create one clean semantic PNG object for a medical social-media infographic.

Topic: {topic}
PNG ID: {task.get('png_id')}
Operation: {task.get('operation')}
Quality strategy: {quality_strategy}

Task:
{instruction}

Must include: {must_include or 'only the semantic object required by the task'}.
Must exclude: {must_exclude or 'all text, watermark, UI, old labels and page background'}.

{style_lock}

Universal output rules:
- transparent or easily removable background outside the object;
- crisp outlines and clear small details; no blur, no soft blob, no painterly smearing;
- no text, letters, numbers, labels, watermark, interface elements or page background;
- neutral educational medical tone; no scary gore unless explicitly required.
""".strip()



def _is_moderation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "moderation_blocked" in text or "safety" in text or "safety system" in text or "safety_violations" in text


def build_safe_semantic_png_prompt(task: dict[str, Any], payload: dict[str, Any]) -> str:
    """Short safety-friendly fallback prompt for medical icon generation."""
    png_id = str(task.get("png_id") or "semantic_png")
    include = ", ".join(str(x) for x in task.get("must_include", [])[:5])
    exclude = ", ".join(str(x) for x in task.get("must_exclude", [])[:5])
    style_lock = _style_lock_text(task, payload)
    return f"""
Create one neutral non-sexual educational medical infographic icon.
No full human body, no intimate body parts, no nudity, no blood, no gore, no text.

ID: {png_id}
Include: {include or 'the requested medical visual object'}.
Avoid: {exclude or 'old labels, UI, watermark, text, background'}.

{style_lock}

Style: clean medical illustration, crisp edges, calm clinic design, transparent-looking square icon.
""".strip()



def _create_fallback_semantic_png(task: dict[str, Any], path: Path, reason: str = "", target_side: int = 512) -> None:
    """Create a local placeholder PNG so one blocked image does not stop the whole pipeline."""
    png_id = str(task.get("png_id") or path.stem)
    entity_id = str(task.get("entity_id") or "")
    side = max(256, min(512, int(target_side or 512)))
    # Draw placeholder in native 512 coordinates, then downscale if needed.
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Medical circular patch
    draw.ellipse((78, 78, 398, 398), fill=(247, 216, 200, 255), outline=(95, 70, 55, 255), width=5)
    draw.ellipse((180, 170, 292, 282), fill=(242, 184, 168, 255))
    draw.ellipse((216, 206, 256, 246), fill=(216, 80, 80, 230))
    # Simple insect icon
    draw.ellipse((318, 300, 388, 346), fill=(55, 45, 35, 255))
    draw.ellipse((362, 306, 430, 352), fill=(65, 55, 45, 255))
    draw.line((330, 300, 305, 260), fill=(55, 45, 35, 255), width=4)
    draw.line((350, 300, 338, 258), fill=(55, 45, 35, 255), width=4)
    draw.line((384, 346, 410, 388), fill=(55, 45, 35, 255), width=4)
    draw.line((405, 345, 450, 376), fill=(55, 45, 35, 255), width=4)
    # Tiny technical marker outside visual area, useful for debugging only
    font = _font(18, bold=True)
    draw.text((18, 470), f"{png_id} fallback", fill=(120, 120, 120, 180), font=font)
    if side != 512:
        img = img.resize((side, side), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")



def _classify_generation_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "moderation_blocked" in text or "safety" in text or "safety_violations" in text:
        return "moderation_blocked"
    if "invalid" in text and "size" in text:
        return "invalid_size"
    if "b64_json" in text or "base64" in text or "не вернул" in text:
        return "no_image_data"
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    if "api key" in text or "authentication" in text or "401" in text:
        return "auth_error"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text or "network" in text:
        return "network_error"
    return "api_error"


def _failure_record(png_id: str, exc: Exception, path: Path | None = None) -> dict[str, str]:
    code = _classify_generation_error(exc)
    message = str(exc).replace("\n", " ").strip()
    return {
        "png_id": png_id,
        "code": code,
        "message": message[:700],
        "path": str(path) if path else "",
    }

def generate_semantic_pngs(asset_id: int, limit: int | None = None) -> tuple[list[str], list[str], dict[str, Any], dict[str, Any]]:
    data = load_analysis(asset_id)
    payload = _payload(data)
    tasks = payload.get("semantic_png_plan") or []
    if not isinstance(tasks, list) or not tasks:
        raise SemanticAssetError("В JSON анализа нет semantic_png_plan.")

    state_id = _state_id(data)
    out_dir = semantic_png_dir(asset_id, state_id)
    _save_blueprint_artifact(asset_id, state_id, payload)
    done: list[str] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []
    cost_items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "extracted": 0,
        "generated_ai": 0,
        "reused": 0,
        "fallback": 0,
        "failed": 0,
        "failed_items": failures,
        "style_lock_groups": _visual_template_groups(payload),
        "image_quality": getattr(settings, "openai_image_quality", "low"),
        "image_input_fidelity": getattr(settings, "openai_image_input_fidelity", "low"),
        "reference_image_used": 0,
        "reference_image_failed": 0,
        "reference_image_items": [],
        "extraction_diagnostics": [],
        "crop_debug_saved": 0,
        "crop_debug_items": [],
    }
    client = _client()
    source_image = _source_image_for_asset(asset_id)

    count = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        png_id = str(task.get("png_id") or f"png_{count+1:03d}")
        output_name = _safe_name(task.get("output_name") or png_id, png_id)
        path = out_dir / f"{output_name}.png"
        # First validate the registry/R2 cache. PostgreSQL metadata alone is not
        # enough: a user can delete objects manually in Cloudflare R2. In that case
        # the stale DB row and local cache must not make the file look reusable.
        _cleanup_stale_semantic_png_rows(asset_id, state_id, output_name, path)
        if path.exists() and path.stat().st_size > 0:
            # Local files are valid only after stale registry rows have been purged.
            _register_artifact_safe(asset_id, state_id, "semantic_png", path)
            skipped.append(str(path))
            stats["reused"] += 1
            continue
        if _restore_semantic_png_from_registry(asset_id, state_id, output_name, path):
            skipped.append(str(path))
            stats["reused"] += 1
            continue
        if limit is not None and count >= limit:
            break

        operation = str(task.get("operation") or "").lower()
        quality_strategy = str(task.get("quality_strategy") or "").lower()
        target_side = _semantic_png_target_size(payload, task)
        api_side = _openai_square_size_for_target(target_side)

        # v43.5: hybrid means "try extraction first, AI only as fallback".
        # This avoids paid regeneration when a UI/watermark issue can be solved by
        # the crop validation + cleanup pipeline.
        should_try_source_extract = (
            operation == "extract_from_source"
            or operation == "hybrid"
            or (operation in {"generate_new", "redraw", "replace"} and quality_strategy in {"hybrid", "try_extract_first"})
        )
        if should_try_source_extract and quality_strategy in {"preserve_original_resolution", "extract_no_upscale", "", "redraw_from_reference", "hybrid", "try_extract_first"}:
            extraction_diags = stats.get("extraction_diagnostics")
            before_diag_count = len(extraction_diags) if isinstance(extraction_diags, list) else 0
            if _extract_source_png(task, source_image, path, extraction_diags):
                _resize_png_down_to_target(path, target_side)
                _register_artifact_safe(asset_id, state_id, "semantic_png", path)
                done.append(str(path))
                stats["extracted"] += 1
                cost_items.append(free_operation(
                    "semantic_png_extract" if operation != "hybrid" else "semantic_png_hybrid_extract_first",
                    {"asset_id": asset_id, "png_id": png_id, "path": str(path), "quality_strategy": quality_strategy, "target_side": target_side, "image_quality": getattr(settings, "openai_image_quality", "low")},
                ))
                count += 1
                continue
            if isinstance(extraction_diags, list) and len(extraction_diags) > before_diag_count:
                last_diag = extraction_diags[-1]
                if isinstance(last_diag, dict) and last_diag.get("status") == "debug_saved":
                    stats["crop_debug_saved"] = int(stats.get("crop_debug_saved") or 0) + 1
                    stats.setdefault("crop_debug_items", []).append({
                        "png_id": png_id,
                        "reason": str(last_diag.get("reason") or "crop_debug_saved"),
                        "debug_path": str(last_diag.get("debug_path") or ""),
                        "raw_touch": last_diag.get("raw_touch") or {},
                    })

        prompts = [
            build_semantic_png_prompt(task, payload),
            build_safe_semantic_png_prompt(task, payload),
        ]
        last_exc: Exception | None = None
        ref_path, ref_group_id, ref_png_id = _reference_png_path_for_task(
            task, payload, asset_id, state_id, out_dir, source_image
        )
        reference_attempted = False

        for prompt in prompts:
            try:
                if ref_path is not None and ref_path.exists() and ref_path.stat().st_size > 0:
                    reference_attempted = True
                    response = _generate_image_with_reference(client, prompt, ref_path, api_side)
                else:
                    response = client.images.generate(
                        model=settings.openai_image_model,
                        prompt=prompt,
                        size=f"{api_side}x{api_side}",
                        n=1,
                        quality=getattr(settings, "openai_image_quality", "low"),
                    )
                image_data = response.data[0]
                b64_json = getattr(image_data, "b64_json", None)
                if not b64_json:
                    raise SemanticAssetError("OpenAI Images API не вернул b64_json.")
                path.write_bytes(base64.b64decode(b64_json))
                _resize_png_down_to_target(path, target_side)
                _register_artifact_safe(asset_id, state_id, "semantic_png", path)
                done.append(str(path))
                stats["generated_ai"] += 1
                if reference_attempted:
                    stats["reference_image_used"] += 1
                    stats["reference_image_items"].append({
                        "png_id": png_id,
                        "group_id": ref_group_id or "",
                        "reference_png_id": ref_png_id or "",
                    })
                cost_items.append(cost_for_image_generation(
                    operation="semantic_png_generate_reference" if reference_attempted else "semantic_png_generate",
                    model=settings.openai_image_model,
                    image_count=1,
                    size=f"{api_side}x{api_side}",
                    metadata={
                        "asset_id": asset_id,
                        "png_id": png_id,
                        "path": str(path),
                        "quality_strategy": quality_strategy,
                        "target_side": target_side,
                        "image_quality": getattr(settings, "openai_image_quality", "low"),
                        "reference_image": bool(reference_attempted),
                        "reference_png_id": ref_png_id or "",
                        "image_input_fidelity": getattr(settings, "openai_image_input_fidelity", "low"),
                    },
                ))
                count += 1
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if reference_attempted:
                    stats["reference_image_failed"] += 1
                    # If reference edit fails, retry the same prompt once as normal
                    # text-to-image generation before trying the shorter safe prompt.
                    try:
                        response = client.images.generate(
                            model=settings.openai_image_model,
                            prompt=prompt,
                            size=f"{api_side}x{api_side}",
                            n=1,
                            quality=getattr(settings, "openai_image_quality", "low"),
                        )
                        image_data = response.data[0]
                        b64_json = getattr(image_data, "b64_json", None)
                        if not b64_json:
                            raise SemanticAssetError("OpenAI Images API не вернул b64_json.")
                        path.write_bytes(base64.b64decode(b64_json))
                        _resize_png_down_to_target(path, target_side)
                        _register_artifact_safe(asset_id, state_id, "semantic_png", path)
                        done.append(str(path))
                        stats["generated_ai"] += 1
                        cost_items.append(cost_for_image_generation(
                            operation="semantic_png_generate_reference_fallback_text",
                            model=settings.openai_image_model,
                            image_count=1,
                            size=f"{api_side}x{api_side}",
                            metadata={"asset_id": asset_id, "png_id": png_id, "path": str(path), "reference_error": str(exc)[:300]},
                        ))
                        count += 1
                        last_exc = None
                        break
                    except Exception as fallback_exc:
                        last_exc = fallback_exc
                # If the first rich prompt fails, retry once with a shorter safe prompt.
                continue

        if last_exc is not None:
            # v42.3: no silent placeholder fallback. A failed AI generation is a
            # real pipeline event that must be visible in Telegram and manifest.
            rec = _failure_record(png_id, last_exc, path)
            failures.append(rec)
            stats["failed"] += 1
            cost_items.append(free_operation(
                "semantic_png_generation_failed",
                {"asset_id": asset_id, "png_id": png_id, "code": rec["code"], "message": rec["message"]},
            ))
            count += 1

    cost_summary = aggregate_costs(cost_items)
    save_cost_event("semantic_png", asset_id, cost_summary)

    manifest = {
        "asset_id": asset_id,
        "project_state_id": _state_id(data),
        "analysis_path": data.get("_analysis_path"),
        "generated": done,
        "skipped_existing": skipped,
        "stats": stats,
        "failures": failures,
        "cost_estimate": cost_summary,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _register_artifact_safe(asset_id, state_id, "manifest", manifest_path)
    return done, skipped, cost_summary, stats

def _matplotlib_font_path(bold: bool = False) -> str | None:
    """Return bundled Matplotlib DejaVu font path if available.

    Railway/Nix images do not always have OS fonts installed. Matplotlib ships
    DejaVu Sans, which supports Cyrillic, so it is a reliable fallback without
    bundling font files in the repository.
    """
    try:
        import matplotlib
        base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        candidate = base / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        if candidate.exists():
            return str(candidate)
    except Exception:
        return None
    return None


def _font_candidates(bold: bool = False) -> list[str]:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    mpl = _matplotlib_font_path(bold=bold)
    if mpl:
        candidates.insert(0, mpl)
    return candidates


def _font_supports_cyrillic(font: ImageFont.ImageFont) -> bool:
    # ImageFont.truetype can load a font that lacks Cyrillic. Avoid silent
    # tofu/boxes by checking that Russian text has a measurable glyph path.
    try:
        mask = font.getmask("Привет")
        return bool(mask.getbbox())
    except Exception:
        return False


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _font_candidates(bold=bold):
        try:
            font = ImageFont.truetype(path, size=size)
            if _font_supports_cyrillic(font):
                return font
        except Exception:
            pass
    # Last-resort fallback: PIL default may not support Cyrillic, but returning
    # it is better than crashing. In normal Railway builds matplotlib is present.
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1])


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
    _, h = _text_size(draw, "Ай", font)
    return max(10, int(h * 1.28))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Word-wrap text. Falls back to character wrapping for long words."""
    text = " ".join(str(text or "").replace("\n", " ").split())
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split():
        test = (current + " " + word).strip()
        width, _ = _text_size(draw, test, font)
        if width <= max_width or not current:
            if width <= max_width:
                current = test
                continue
            # Very long word: split by characters.
            part = ""
            for ch in word:
                cand = part + ch
                w, _ = _text_size(draw, cand, font)
                if w <= max_width or not part:
                    part = cand
                else:
                    if current:
                        lines.append(current)
                        current = ""
                    lines.append(part)
                    part = ch
            current = part
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    bold: bool = False,
    max_lines: int | None = None,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    """Find the largest Cyrillic font size that fits the box.

    The function prefers fitting the complete text. If the text cannot fit even
    at min_size, it returns as many full lines as fit and appends an ellipsis to
    the last visible line. This prevents text from being drawn outside a card.
    """
    max_width = max(20, int(max_width))
    max_height = max(12, int(max_height))
    text = str(text or "").strip()
    for size in range(int(max_size), int(min_size) - 1, -1):
        font = _font(size, bold=bold)
        lines = _wrap(draw, text, font, max_width)
        if max_lines is not None:
            lines = lines[:max_lines]
        lh = _line_height(draw, font)
        if lines and len(lines) * lh <= max_height:
            return font, lines, lh

    font = _font(min_size, bold=bold)
    all_lines = _wrap(draw, text, font, max_width)
    lh = _line_height(draw, font)
    allowed = max(1, max_height // lh)
    if max_lines is not None:
        allowed = min(allowed, max_lines)
    lines = all_lines[:allowed]
    if len(all_lines) > allowed and lines:
        last = lines[-1]
        while last and _text_size(draw, last + "…", font)[0] > max_width:
            last = last[:-1]
        lines[-1] = (last.rstrip() + "…") if last else "…"
    return font, lines or [""], lh


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    fill: str,
    bold: bool = False,
    max_lines: int | None = None,
) -> int:
    font, lines, lh = _fit_text(draw, text, max_width, max_height, max_size, min_size, bold=bold, max_lines=max_lines)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += lh
    return y


def _infer_grid(card_count: int, layout_text: str) -> tuple[int, int]:
    """Infer card grid from blueprint text and card count."""
    layout_norm = (layout_text or "").lower().replace(" ", "")
    if any(key in layout_norm for key in ["4×4", "4x4", "4колонки", "4колон"]):
        return 4, max(1, math.ceil(card_count / 4))
    if any(key in layout_norm for key in ["2×5", "2x5", "2колонки", "2колон"]):
        return 2, max(1, math.ceil(card_count / 2))
    if card_count <= 6:
        return 2, max(1, math.ceil(card_count / 2))
    if card_count <= 10:
        return 2, max(1, math.ceil(card_count / 2))
    if card_count <= 16:
        return 4, max(1, math.ceil(card_count / 4))
    return 4, max(1, math.ceil(card_count / 4))




# v43.2 Smart Blocks Composer -------------------------------------------------
SMART_BLOCK_DEFAULTS = {
    "when_doctor": {"title": "Когда к врачу", "icon_role": "doctor", "color_role": "danger"},
    "first_aid": {"title": "Что сделать сразу", "icon_role": "first_aid", "color_role": "care"},
    "prevention": {"title": "Как защититься", "icon_role": "shield", "color_role": "safe"},
    "important_note": {"title": "Важно", "icon_role": "info", "color_role": "warning"},
    "contraindications": {"title": "Когда нужна консультация", "icon_role": "alert", "color_role": "danger"},
    "how_to_choose": {"title": "Как выбрать", "icon_role": "check", "color_role": "info"},
    "checklist": {"title": "Проверьте себя", "icon_role": "check", "color_role": "info"},
    "normal_variability": {"title": "Что может быть нормой", "icon_role": "info", "color_role": "safe"},
    "screening_note": {"title": "Обследования", "icon_role": "doctor", "color_role": "info"},
}

SMART_BLOCK_COLORS = {
    "danger": {"bg": "#FFE3E0", "accent": "#C83F3F", "text": "#3A1D1D"},
    "safe": {"bg": "#EAF6E8", "accent": "#2E7D32", "text": "#1F3B24"},
    "care": {"bg": "#FFF4D8", "accent": "#D99000", "text": "#4A3512"},
    "info": {"bg": "#EAF2FF", "accent": "#2F6FDB", "text": "#1E3657"},
    "warning": {"bg": "#FFF1CC", "accent": "#B56A00", "text": "#3D2A09"},
    "neutral": {"bg": "#F7F7F7", "accent": "#333333", "text": "#222222"},
}

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}



def _footer_contains_important_note(payload: dict[str, Any]) -> bool:
    try:
        custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
        cp = custom.get("content_pack") if isinstance(custom.get("content_pack"), dict) else {}
        blocks = cp.get("footer_blocks") or []
        if not isinstance(blocks, list):
            return False
        blob = " ".join([str((b or {}).get("title", "")) + " " + str((b or {}).get("text", "")) for b in blocks if isinstance(b, dict)]).lower()
        return any(x in blob for x in ["важно", "не диагноз", "не заменяет", "112", "срочно", "врач"])
    except Exception:
        return False

def _smart_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
    raw = custom.get("smart_blocks") or payload.get("smart_blocks") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    footer_has_important_note = _footer_contains_important_note(payload)
    for block in raw:
        if len(out) >= 3:
            break
        if not isinstance(block, dict):
            continue
        btype = str(block.get("block_type") or "important_note").lower()
        if btype not in SMART_BLOCK_DEFAULTS:
            btype = "important_note"
        # If legal/safety footer already contains the generic important note,
        # do not waste layout space by duplicating it as a smart block.
        if btype == "important_note" and footer_has_important_note:
            continue
        if btype in seen:
            continue
        defaults = SMART_BLOCK_DEFAULTS[btype]
        priority = str(block.get("priority") or "medium").lower()
        if priority not in PRIORITY_ORDER:
            priority = "medium"
        items = block.get("items") if isinstance(block.get("items"), list) else []
        clean_items = []
        for item in items:
            txt = " ".join(str(item or "").split())
            if txt:
                clean_items.append(txt[:92])
            if len(clean_items) >= 4:
                break
        if not clean_items:
            continue
        out.append({
            "block_id": str(block.get("block_id") or f"smart_{len(out)+1:03d}"),
            "block_type": btype,
            "priority": priority,
            "title": str(block.get("title") or defaults["title"])[:48],
            "items": clean_items,
            "icon_role": str(block.get("icon_role") or defaults["icon_role"]).lower(),
            "color_role": str(block.get("color_role") or defaults["color_role"]).lower(),
        })
        seen.add(btype)
    out.sort(key=lambda b: PRIORITY_ORDER.get(str(b.get("priority")), 1))
    return out


def _select_smart_blocks_for_layout(blocks: list[dict[str, Any]], card_count: int) -> list[dict[str, Any]]:
    if not blocks:
        return []
    if card_count >= 14:
        return [b for b in blocks if str(b.get("priority")) == "high"][:1] or blocks[:1]
    if card_count > 10:
        return blocks[:2]
    # For typical social infographics with up to 10 cards, three compact
    # blocks give the best information gain: when_doctor + first_aid + prevention.
    return blocks[:3]


def _draw_simple_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, icon_role: str, accent: str) -> None:
    # Minimal vector-like icons drawn with Pillow. They are deliberately simple,
    # free and predictable; AI should not generate decorative UI elements.
    icon_role = (icon_role or "info").lower()
    draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill="#FFFFFF", outline=accent, width=max(2, r//8))
    if icon_role in {"alert", "info"}:
        draw.text((cx-5, cy-r//2), "!" if icon_role == "alert" else "i", fill=accent, font=_font(max(16, r), bold=True))
    elif icon_role in {"shield", "doctor"}:
        pts = [(cx, cy-r+4), (cx+r-5, cy-r//4), (cx+r//2, cy+r-4), (cx, cy+r-1), (cx-r//2, cy+r-4), (cx-r+5, cy-r//4)]
        draw.polygon(pts, outline=accent, fill=None)
        draw.line((cx-r//3, cy, cx+r//3, cy), fill=accent, width=max(2, r//8))
        draw.line((cx, cy-r//3, cx, cy+r//3), fill=accent, width=max(2, r//8))
    elif icon_role == "first_aid":
        draw.rounded_rectangle((cx-r//2, cy-r//3, cx+r//2, cy+r//2), radius=4, outline=accent, width=max(2, r//8))
        draw.line((cx-r//4, cy+2, cx+r//4, cy+2), fill=accent, width=max(2, r//8))
        draw.line((cx, cy-r//4, cx, cy+r//3), fill=accent, width=max(2, r//8))
    elif icon_role == "check":
        draw.line((cx-r//2, cy, cx-r//8, cy+r//3, cx+r//2, cy-r//3), fill=accent, width=max(3, r//7), joint="curve")
    elif icon_role == "phone":
        draw.arc((cx-r//2, cy-r//2, cx+r//2, cy+r//2), 110, 260, fill=accent, width=max(3, r//7))
    elif icon_role in {"pill", "thermometer", "leaf", "heart"}:
        draw.rounded_rectangle((cx-r//2, cy-r//5, cx+r//2, cy+r//5), radius=r//5, outline=accent, width=max(2, r//8))
    else:
        draw.text((cx-5, cy-r//2), "i", fill=accent, font=_font(max(16, r), bold=True))


def _draw_smart_block(draw: ImageDraw.ImageDraw, block: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    color = SMART_BLOCK_COLORS.get(str(block.get("color_role") or "info"), SMART_BLOCK_COLORS["info"])
    btype = str(block.get("block_type") or "").lower()
    border_w = 4 if btype == "when_doctor" else 2
    draw.rounded_rectangle(box, radius=22, fill=color["bg"], outline=color["accent"], width=border_w)
    icon_r = max(15, min(24, (y2-y1)//5))
    icon_cx = x1 + 26 + icon_r
    icon_cy = y1 + 26 + icon_r
    _draw_simple_icon(draw, icon_cx, icon_cy, icon_r, str(block.get("icon_role") or "info"), color["accent"])
    title_x = icon_cx + icon_r + 14
    title_y = y1 + 18
    title_w = max(80, x2 - title_x - 18)
    display_title = str(block.get("title") or "Важно")
    if btype == "when_doctor" and "112" not in display_title:
        display_title = display_title + " / 112"
    title_end = _draw_fitted_text(draw, (title_x, title_y), display_title, title_w, 34, 22, 13, color["accent"], bold=True, max_lines=1)
    items = block.get("items") if isinstance(block.get("items"), list) else []
    body = " • ".join([str(x) for x in items[:4] if str(x).strip()])
    _draw_fitted_text(draw, (x1 + 22, max(title_end + 8, y1 + 58)), body, x2 - x1 - 44, max(24, y2 - y1 - 70), 18, 10, color["text"], bold=False, max_lines=4)


def _draw_smart_blocks_zone(draw: ImageDraw.ImageDraw, blocks: list[dict[str, Any]], x: int, y: int, w: int, h: int) -> None:
    if not blocks or h <= 40:
        return
    n = len(blocks)
    gap = 16
    if n == 1:
        _draw_smart_block(draw, blocks[0], (x, y, x+w, y+h))
    elif n == 2:
        bw = (w-gap)//2
        _draw_smart_block(draw, blocks[0], (x, y, x+bw, y+h))
        _draw_smart_block(draw, blocks[1], (x+bw+gap, y, x+w, y+h))
    else:
        top_h = (h-gap)//2
        bw = (w-gap)//2
        _draw_smart_block(draw, blocks[0], (x, y, x+bw, y+top_h))
        _draw_smart_block(draw, blocks[1], (x+bw+gap, y, x+w, y+top_h))
        _draw_smart_block(draw, blocks[2], (x, y+top_h+gap, x+w, y+h))



# v43.5 Dynamic Card Heights --------------------------------------------------
def _card_content_weight(card: dict[str, Any]) -> int:
    """Estimate how much vertical space a card needs.

    This is intentionally cheap and deterministic. It does not ask AI again; it
    simply allocates more row height to cards that contain longer text and
    examples[].
    """
    title = str(card.get("title") or card.get("card_id") or "")
    body = str(card.get("short_text") or "")
    examples = card.get("examples") if isinstance(card.get("examples"), list) else []
    ex_text = " • ".join([str(x).strip() for x in examples if str(x).strip()])
    # Titles are usually one line, examples are compact but important source
    # facts; they must reserve their own space rather than being silently cut.
    return max(60, len(title) * 2 + len(body) + len(ex_text) + (55 if ex_text else 0))


def _dynamic_row_heights(cards: list[dict[str, Any]], cols: int, rows: int, available_h: int, gap_y: int) -> list[int]:
    """Allocate different heights to rows based on the densest card in each row."""
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    total_cards_h = max(120 * rows, int(available_h) - gap_y * (rows - 1))
    row_weights: list[int] = []
    for row in range(rows):
        row_cards = cards[row * cols:(row + 1) * cols]
        if not row_cards:
            row_weights.append(60)
        else:
            row_weights.append(max(_card_content_weight(c) for c in row_cards))

    min_h = 132 if cols <= 2 else 118
    weights_sum = max(1, sum(row_weights))
    heights = [max(min_h, int(total_cards_h * w / weights_sum)) for w in row_weights]

    # If rounding/minimums exceeded the available space, reduce from the most
    # spacious rows first, but never below min_h.
    overflow = sum(heights) - total_cards_h
    while overflow > 0 and any(h > min_h for h in heights):
        i = max(range(len(heights)), key=lambda idx: heights[idx])
        delta = min(overflow, heights[i] - min_h)
        heights[i] -= delta
        overflow -= delta

    # If there is spare space, give it to rows with the largest content weight.
    spare = total_cards_h - sum(heights)
    if spare > 0 and heights:
        order = sorted(range(len(heights)), key=lambda idx: row_weights[idx], reverse=True)
        for n in range(spare):
            heights[order[n % len(order)]] += 1
    return heights

def _png_lookup(asset_id: int, state_id: int | None, payload: dict[str, Any]) -> dict[str, Path]:

    base = semantic_png_dir(asset_id, state_id)
    lookup: dict[str, Path] = {}
    for task in payload.get("semantic_png_plan") or []:
        if not isinstance(task, dict):
            continue
        png_id = str(task.get("png_id") or "")
        if not png_id:
            continue
        candidates = list(base.glob(f"{_safe_name(png_id)}*.png")) + list(base.glob(f"*{_safe_name(png_id)}*.png"))
        if candidates:
            lookup[png_id] = candidates[0]
            continue
        output_name = _safe_name(task.get("output_name") or png_id, png_id)
        desired = base / f"{output_name}.png"
        if _restore_semantic_png_from_registry(asset_id, state_id, output_name, desired):
            lookup[png_id] = desired
    return lookup




def _hero_png_ids(payload: dict[str, Any]) -> list[str]:
    """Return semantic PNG ids for universal hero_visual entities.

    A hero_visual is a non-repeating main visual anchor that explains the topic
    of the whole infographic. It is drawn in the header zone, not as a card.
    """
    entities = payload.get("visual_entity_map") if isinstance(payload.get("visual_entity_map"), list) else []
    hero_entity_ids = {
        str(e.get("entity_id"))
        for e in entities
        if isinstance(e, dict) and str(e.get("entity_role") or "").lower() == "hero_visual" and e.get("entity_id")
    }
    if not hero_entity_ids:
        custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else {}
        entities = custom.get("visual_entity_map") if isinstance(custom.get("visual_entity_map"), list) else []
        hero_entity_ids = {
            str(e.get("entity_id"))
            for e in entities
            if isinstance(e, dict) and str(e.get("entity_role") or "").lower() == "hero_visual" and e.get("entity_id")
        }
    out: list[str] = []
    for task in payload.get("semantic_png_plan") or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("entity_id") or "") in hero_entity_ids and task.get("png_id"):
            out.append(str(task.get("png_id")))
    return out[:2]



def delete_semantic_png_artifacts(asset_id: int, target: str | None = None) -> dict[str, Any]:
    """Delete cached Semantic PNG artifacts for an asset.

    target can be None/"all" for the full package, or a png id/file stem such as
    png_002. Deletes local files, R2 objects registered in PostgreSQL, DB rows,
    and the local manifest for the current analysis state.
    """
    data = load_analysis(asset_id)
    state_id = _state_id(data)
    base = semantic_png_dir(asset_id, state_id)
    raw_target = (target or "").strip()
    delete_all = raw_target == "" or raw_target.lower() in {"all", "все", "пакет", "package", "*"}
    file_target = None if delete_all else raw_target

    stats = {
        "asset_id": asset_id,
        "project_state_id": state_id,
        "target": "all" if delete_all else raw_target,
        "local_deleted": 0,
        "remote_deleted": 0,
        "db_deleted": 0,
        "matched": 0,
        "manifest_deleted": 0,
        "errors": [],
        "deleted_files": [],
    }

    # Delete registered semantic PNG rows and corresponding R2/local files.
    try:
        reg_stats = delete_artifacts(
            asset_id=asset_id,
            state_id=state_id,
            kind="semantic_png",
            file_name_or_prefix=file_target,
            delete_remote=True,
            delete_local=True,
        )
        stats["matched"] += int(reg_stats.get("matched", 0))
        stats["local_deleted"] += int(reg_stats.get("local_deleted", 0))
        stats["remote_deleted"] += int(reg_stats.get("remote_deleted", 0))
        stats["db_deleted"] += int(reg_stats.get("db_deleted", 0))
        stats["deleted_files"].extend(reg_stats.get("deleted_files", []) or [])
        if reg_stats.get("remote_errors"):
            stats["errors"].append(f"remote_errors={reg_stats.get('remote_errors')}")
        if reg_stats.get("local_errors"):
            stats["errors"].append(f"local_errors={reg_stats.get('local_errors')}")
    except Exception as exc:
        stats["errors"].append(f"registry_delete_error: {exc}")

    # Also delete local files that may exist but were not registered, for old deployments.
    try:
        if base.exists():
            patterns = ["*.png"] if delete_all else [f"{raw_target}.png", f"{raw_target}*.png"]
            seen: set[Path] = set()
            for pattern in patterns:
                for p in base.glob(pattern):
                    if p in seen or not p.is_file():
                        continue
                    seen.add(p)
                    if p.name not in stats["deleted_files"]:
                        stats["deleted_files"].append(p.name)
                        stats["matched"] += 1
                    try:
                        p.unlink()
                        stats["local_deleted"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"local_delete_error:{p.name}: {exc}")
            if delete_all:
                manifest = base / "manifest.json"
                if manifest.exists() and manifest.is_file():
                    manifest.unlink()
                    stats["manifest_deleted"] += 1
    except Exception as exc:
        stats["errors"].append(f"local_scan_error: {exc}")

    # Delete registered manifest only when removing the entire package.
    if delete_all:
        try:
            man_stats = delete_artifacts(
                asset_id=asset_id,
                state_id=state_id,
                kind="manifest",
                file_name_or_prefix=None,
                delete_remote=True,
                delete_local=True,
            )
            stats["remote_deleted"] += int(man_stats.get("remote_deleted", 0))
            stats["db_deleted"] += int(man_stats.get("db_deleted", 0))
            stats["manifest_deleted"] += int(man_stats.get("matched", 0))
        except Exception as exc:
            stats["errors"].append(f"manifest_delete_error: {exc}")

    return stats


def list_semantic_png_paths(asset_id: int) -> list[Path]:
    """Return generated semantic PNG files for the latest saved analysis/state."""
    data = load_analysis(asset_id)
    state_id = _state_id(data)
    base = semantic_png_dir(asset_id, state_id)
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
    local = sorted([p for p in base.glob("*.png") if p.is_file()], key=lambda p: p.name)
    if local:
        return local
    # Try restoring ZIP contents from R2 registry when Railway local disk is empty.
    try:
        for row in list_artifacts(asset_id, state_id, kind="semantic_png"):
            ensure_artifact_local(row, base / row.file_name)
    except Exception:
        pass
    return sorted([p for p in base.glob("*.png") if p.is_file()], key=lambda p: p.name)



def _estimate_card_weight_for_width(draw: ImageDraw.ImageDraw, card: dict[str, Any], text_w: int) -> int:
    """Estimate card height need for a given width.

    Used by the v43.5 layout engine before drawing. It keeps examples[] as
    first-class content and prevents long Russian titles from being squeezed
    into very narrow boxes when a wider final-row card would work better.
    """
    title = str(card.get("title") or card.get("card_id") or "")
    body = str(card.get("short_text") or "")
    examples = card.get("examples") if isinstance(card.get("examples"), list) else []
    examples_text = " • ".join([str(x).strip() for x in examples if str(x).strip()])
    title_lines = len(_wrap(draw, title, _font(24, bold=True), max(80, text_w)))
    body_lines = len(_wrap(draw, body, _font(18), max(80, text_w)))
    ex_lines = len(_wrap(draw, "Примеры: " + examples_text, _font(14, bold=True), max(80, text_w))) if examples_text else 0
    return 88 + title_lines * 24 + body_lines * 18 + ex_lines * 17


def _draw_content_card(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    card: dict[str, Any],
    pngs: dict[str, Path],
    x: int,
    cy: int,
    card_w: int,
    card_h: int,
    horizontal: bool = True,
) -> None:
    """Draw a single dynamic card.

    v43.5: the card receives an explicit box from the layout engine. This lets
    rows have different heights and lets the last card span full width when that
    gives better text readability.
    """
    draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=22, fill="#FFFFFF", outline="#DCEAF7", width=2)
    png_id = str(card.get("png_id") or "")
    png_path = pngs.get(png_id)

    if horizontal and card_w >= 390:
        icon_box = min(132, max(78, card_h - 32), max(86, card_w // 3))
        icon_x = x + 18
        icon_y = cy + max(12, (card_h - icon_box) // 2)
        text_x = x + icon_box + 38
        text_y = cy + 20
        text_w = max(120, card_w - icon_box - 60)
        title_h = max(30, min(58, int(card_h * 0.27)))
        title_max, title_min = 28, 15
        body_max, body_min = 21, 12
    else:
        icon_box = min(max(64, int(card_h * 0.36)), card_w - 28)
        icon_x = x + (card_w - icon_box) // 2
        icon_y = cy + 12
        text_x = x + 14
        text_y = icon_y + icon_box + 8
        text_w = card_w - 28
        title_h = max(24, min(48, int((card_h - icon_box - 28) * 0.35)))
        title_max, title_min = 20, 10
        body_max, body_min = 15, 9

    if png_path and png_path.exists():
        try:
            p_img = Image.open(png_path).convert("RGBA")
            p_img.thumbnail((icon_box, icon_box), Image.Resampling.LANCZOS)
            px = icon_x + (icon_box - p_img.width) // 2
            py = icon_y + (icon_box - p_img.height) // 2
            img.paste(p_img, (px, py), p_img)
        except Exception:
            draw.ellipse((icon_x, icon_y, icon_x + icon_box, icon_y + icon_box), fill="#E9F5FF", outline="#B8D6EE")
    else:
        draw.ellipse((icon_x, icon_y, icon_x + icon_box, icon_y + icon_box), fill="#E9F5FF", outline="#B8D6EE")

    title = str(card.get("title") or card.get("card_id") or "")
    short_text = str(card.get("short_text") or "")
    examples = card.get("examples") if isinstance(card.get("examples"), list) else []
    examples = [str(x).strip() for x in examples if str(x).strip()]
    examples_text = " • ".join(examples[:5])

    title_end = _draw_fitted_text(
        draw,
        (text_x, text_y),
        title,
        text_w,
        title_h,
        max_size=title_max,
        min_size=title_min,
        fill="#1E1E1E",
        bold=True,
        max_lines=2,
    )
    body_y = min(title_end + 4, text_y + title_h)
    examples_h = 36 if examples_text else 0
    body_box_h = max(18, cy + card_h - body_y - 12 - examples_h)
    _draw_fitted_text(
        draw,
        (text_x, body_y),
        short_text,
        text_w,
        body_box_h,
        max_size=body_max,
        min_size=body_min,
        fill="#444444",
        bold=False,
        max_lines=None,
    )
    if examples_text:
        ex_y = cy + card_h - examples_h - 8
        _draw_fitted_text(
            draw,
            (text_x, ex_y),
            "Примеры: " + examples_text,
            text_w,
            examples_h,
            max_size=max(12, body_max - 2),
            min_size=max(8, body_min - 2),
            fill="#0B6FAE",
            bold=True,
            max_lines=2,
        )

def compose_reconstruction(asset_id: int) -> str:
    data = load_analysis(asset_id)
    payload = _payload(data)
    state_id = _state_id(data)
    bp = payload.get("design_blueprint") if isinstance(payload.get("design_blueprint"), dict) else {}
    canvas = bp.get("canvas") if isinstance(bp.get("canvas"), dict) else {}
    width = int(canvas.get("width") or 1080)
    height = int(canvas.get("height") or 1350)

    img = Image.new("RGB", (width, height), "#FFF7E8")
    draw = ImageDraw.Draw(img)

    pack = _content_pack(payload)
    header = pack.get("header") if isinstance(pack.get("header"), dict) else {}
    bp_header = bp.get("header") if isinstance(bp.get("header"), dict) else {}
    header_text = (
        header.get("text")
        or bp_header.get("text")
        or payload.get("analysis_state", {}).get("topic")
        or "Медицинская памятка"
    )
    subtitle = header.get("subtitle") or bp_header.get("subtitle") or "Это ориентиры, не диагноз. Важны симптомы и обстоятельства"

    cards = pack.get("cards") if isinstance(pack.get("cards"), list) else []
    cards = [c for c in cards if isinstance(c, dict)]
    smart_blocks_all = _smart_blocks(payload)
    smart_blocks = _select_smart_blocks_for_layout(smart_blocks_all, len(cards))
    pngs = _png_lookup(asset_id, state_id, payload)
    hero_png_ids = _hero_png_ids(payload)
    hero_paths = [pngs.get(pid) for pid in hero_png_ids if pngs.get(pid) and pngs.get(pid).exists()]

    # Header area: text is fitted into the available block instead of being
    # drawn with a fixed font that can overflow on long Russian headings.
    # v43.4: if a hero_visual exists, reserve a right-side hero zone; it is
    # a visual anchor for the whole topic and must not become a normal card.
    header_x = 60
    header_y = 26
    # v43.5: hero is no longer a tiny corner icon. If present, reserve a
    # real hero zone (about 25–30% of the header width) so a topic anchor like
    # "nose + bottle" remains recognizable.
    hero_box_w = 300 if hero_paths else 0
    header_w = width - 120 - (hero_box_w + 28 if hero_paths else 0)
    header_h = 170 if hero_paths else 138
    title_end_y = _draw_fitted_text(
        draw,
        (header_x, header_y),
        str(header_text),
        header_w,
        92,
        max_size=56,
        min_size=30,
        fill="#1E1E1E",
        bold=True,
        max_lines=2,
    )
    _draw_fitted_text(
        draw,
        (header_x, min(title_end_y + 8, header_y + 98)),
        str(subtitle),
        header_w,
        42,
        max_size=26,
        min_size=16,
        fill="#2F6F5E",
        bold=False,
        max_lines=2,
    )
    if hero_paths:
        hx = width - 60 - hero_box_w
        hy = 24
        hw = hero_box_w
        hh = header_h
        for hp in hero_paths[:1]:
            try:
                h_img = Image.open(hp).convert("RGBA")
                h_img.thumbnail((hw, hh), Image.Resampling.LANCZOS)
                px = hx + (hw - h_img.width) // 2
                py = hy + (hh - h_img.height) // 2
                img.paste(h_img, (px, py), h_img)
            except Exception:
                pass

    layout_text = str(bp.get("layout") or "")
    cols, rows = _infer_grid(len(cards), layout_text)
    cols = max(1, cols)
    rows = max(1, rows)

    margin_x = 50
    gap_x = 24 if cols <= 2 else 18
    gap_y = 18 if rows <= 5 else 12
    footer_bottom = 38
    footer_h = 128 if smart_blocks_all else 150

    # v43.5 Dynamic Layout Engine -----------------------------------------
    # Composer now plans the whole vertical layout instead of blindly drawing
    # everything into a fixed grid. Priorities:
    # 1) keep all cards and examples[] readable;
    # 2) keep a real hero zone when hero_visual exists;
    # 3) keep when_doctor / footer;
    # 4) reduce smart blocks before squeezing card text.
    start_y = header_y + header_h + 22

    # Re-select smart blocks by available content load. Up to 10 cards can show
    # three blocks only when card count is small enough; otherwise lower-priority
    # blocks are trimmed before cards are compressed.
    if len(cards) <= 5:
        smart_blocks = _select_smart_blocks_for_layout(smart_blocks_all, len(cards))[:3]
    elif len(cards) <= 10:
        smart_blocks = _select_smart_blocks_for_layout(smart_blocks_all, len(cards))[:2]
    else:
        smart_blocks = _select_smart_blocks_for_layout(smart_blocks_all, len(cards))[:1]

    def _smart_height(n: int) -> int:
        if n <= 0:
            return 0
        if n == 1:
            return 116
        if n == 2:
            return 150
        return 208

    smart_h = _smart_height(len(smart_blocks))
    smart_gap = 16 if smart_blocks else 0
    footer_y = height - footer_bottom - footer_h
    smart_y = footer_y - smart_gap - smart_h if smart_blocks else footer_y
    available_h = max(250, smart_y - start_y - 18)

    # If there is still not enough room, drop medium/low smart blocks one by one
    # before sacrificing examples[] or footer readability.
    min_reasonable_card_zone = 500 if len(cards) <= 5 else 650 if len(cards) <= 10 else 720
    while smart_blocks and available_h < min_reasonable_card_zone:
        smart_blocks = smart_blocks[:-1]
        smart_h = _smart_height(len(smart_blocks))
        smart_gap = 16 if smart_blocks else 0
        smart_y = footer_y - smart_gap - smart_h if smart_blocks else footer_y
        available_h = max(250, smart_y - start_y - 18)

    card_boxes: list[tuple[dict[str, Any], int, int, int, int, bool]] = []

    # Special high-quality medical layout: 5 cards with hero. This is common for
    # classification infographics such as drops/sprays. Layout is 2+2+1: the
    # final card spans the full width, giving long examples and warnings room.
    if len(cards) == 5 and width >= 900:
        cols = 2
        half_w = (width - margin_x * 2 - gap_x) // 2
        full_w = width - margin_x * 2
        row_weights: list[int] = []
        rows_cards = [cards[0:2], cards[2:4], cards[4:5]]
        for i, row_cards in enumerate(rows_cards):
            probe_w = full_w - 210 if i == 2 else half_w - 190
            row_weights.append(max(_estimate_card_weight_for_width(draw, c, probe_w) for c in row_cards))
        total_gap = gap_y * 2
        total_h = max(360, available_h - total_gap)
        min_heights = [150, 150, 170]
        wsum = max(1, sum(row_weights))
        heights = [max(min_heights[i], int(total_h * row_weights[i] / wsum)) for i in range(3)]
        # Normalize to available space.
        overflow = sum(heights) - total_h
        while overflow > 0 and max(heights) > 135:
            idx = heights.index(max(heights))
            step = min(overflow, max(1, heights[idx] - min_heights[idx]))
            if step <= 0:
                break
            heights[idx] -= step
            overflow -= step
        y0 = start_y
        for r in range(2):
            h = heights[r]
            for c in range(2):
                idx = r * 2 + c
                x = margin_x + c * (half_w + gap_x)
                card_boxes.append((cards[idx], x, y0, half_w, h, True))
            y0 += h + gap_y
        card_boxes.append((cards[4], margin_x, y0, full_w, heights[2], True))
    else:
        card_w = (width - margin_x * 2 - gap_x * (cols - 1)) // cols
        max_cards = cols * rows
        if len(cards) > max_cards:
            rows = math.ceil(len(cards) / cols)
        row_heights = _dynamic_row_heights(cards, cols, rows, available_h, gap_y)
        row_y_positions: list[int] = []
        acc_y = start_y
        for rh in row_heights:
            row_y_positions.append(acc_y)
            acc_y += rh + gap_y
        for idx, card in enumerate(cards):
            col = idx % cols
            row = idx // cols
            if row >= rows:
                break
            x = margin_x + col * (card_w + gap_x)
            card_boxes.append((card, x, row_y_positions[row], card_w, row_heights[row], cols <= 2))

    for card, x, cy, card_w, card_h, horizontal in card_boxes:
        _draw_content_card(img, draw, card, pngs, x, cy, card_w, card_h, horizontal=horizontal)

    if smart_blocks:
        _draw_smart_blocks_zone(draw, smart_blocks, 50, smart_y, width - 100, smart_h)

    footer_blocks = pack.get("footer_blocks") if isinstance(pack.get("footer_blocks"), list) else []
    if footer_blocks and isinstance(footer_blocks[0], dict):
        ftitle = footer_blocks[0].get("title") or "Срочно за помощью"
        ftext = footer_blocks[0].get("text") or "Одышка, отек лица или горла, слабость, быстро растущее покраснение, гной, лихорадка."
    else:
        ftitle = "Срочно за помощью"
        ftext = "Одышка, отек лица или горла, слабость, быстро растущее покраснение, гной, лихорадка."

    draw.rounded_rectangle((50, footer_y, width - 50, height - footer_bottom), radius=28, fill="#FFE7E3")
    _draw_fitted_text(draw, (82, footer_y + 22), "!", 34, 50, 42, 24, fill="#C83F3F", bold=True, max_lines=1)
    _draw_fitted_text(draw, (128, footer_y + 26), str(ftitle), width - 200, 38, 28, 16, fill="#C83F3F", bold=True, max_lines=1)
    _draw_fitted_text(draw, (128, footer_y + 68), str(ftext), width - 190, footer_h - 78, 22, 12, fill="#1E1E1E", bold=False, max_lines=4)

    out = reconstruction_dir() / f"asset-{asset_id}-state-{state_id or 'latest'}-reconstruction.png"
    img.save(out, "PNG")
    _register_artifact_safe(asset_id, state_id, "reconstruction", out)
    _save_blueprint_artifact(asset_id, state_id, payload)
    return str(out)
