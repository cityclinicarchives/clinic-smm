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
from app.services.semantic_asset_store import register_artifact, list_artifacts, ensure_artifact_local


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


def _normalized_crop_box(task: dict[str, Any], image: Image.Image) -> tuple[int, int, int, int] | None:
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
    return _expand_crop_box((left, top, right, bottom), image.size, task)


def _expand_crop_box(box: tuple[int, int, int, int], image_size: tuple[int, int], task: dict[str, Any]) -> tuple[int, int, int, int]:
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
    # Higher confidence boxes get a slightly smaller margin; low confidence boxes need more room.
    ratio = 0.035 if confidence >= 0.75 else 0.055
    pad_x = max(6, int(crop_w * ratio))
    pad_y = max(6, int(crop_h * ratio))
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


def _component_cleanup_and_trim(img: Image.Image) -> Image.Image:
    """Remove detached text rows/artifacts and trim to the foreground object.

    v41 line-based text suppression.

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

def _remove_flat_corner_background(crop: Image.Image) -> Image.Image:
    """Backward-compatible wrapper for the new edge-aware extractor."""
    return _remove_edge_aware_background(crop)


def _remove_text_like_edge_fragments(img: Image.Image) -> Image.Image:
    """Backward-compatible wrapper for component cleanup and trimming."""
    return _component_cleanup_and_trim(img)

def _extract_source_png(task: dict[str, Any], source_image: Image.Image | None, path: Path) -> bool:
    if source_image is None:
        return False
    box = _normalized_crop_box(task, source_image)
    if box is None:
        return False
    crop = source_image.crop(box)
    # Do not upscale. Save native crop resolution. Optionally remove flat background and stray text.
    if bool(task.get('transparent_background', True)):
        crop = _remove_flat_corner_background(crop)
        crop = _remove_text_like_edge_fragments(crop)
    path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(path, 'PNG')
    return True


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


def _restore_semantic_png_from_registry(asset_id: int, state_id: int | None, output_name: str, local_path: Path) -> bool:
    """Restore generated Semantic PNG from PostgreSQL/R2 registry when local file is missing."""
    try:
        rows = list_artifacts(asset_id, state_id, kind="semantic_png")
        wanted = {local_path.name, f"{output_name}.png"}
        for row in rows:
            if row.file_name in wanted or row.file_name.startswith(output_name):
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


def build_semantic_png_prompt(task: dict[str, Any], payload: dict[str, Any]) -> str:
    analysis_state = payload.get("analysis_state") if isinstance(payload.get("analysis_state"), dict) else {}
    topic = analysis_state.get("topic") or "медицинская инфографика"
    instruction = task.get("instruction_for_python_or_image_ai") or "Создать смысловой PNG-объект для инфографики."
    must_include = ", ".join(str(x) for x in task.get("must_include", [])[:12])
    must_exclude = ", ".join(str(x) for x in task.get("must_exclude", [])[:12])
    reference_png_id = task.get("reference_png_id") or "лучший сохраненный исходный PNG"
    quality_strategy = task.get("quality_strategy") or "regenerate_high_detail"
    return f"""
Создай один чистый смысловой PNG-объект для медицинской SMM-инфографики.

Тема инфографики: {topic}
PNG ID: {task.get('png_id')}
Операция по плану: {task.get('operation')}
Quality strategy: {quality_strategy}
Style reference: {reference_png_id}

Задание:
{instruction}

Обязательно включить: {must_include or 'только смысловой объект по заданию'}.
Обязательно исключить: {must_exclude or 'любой текст, watermark, кнопки интерфейса, старый фон'}.

СТРОГИЙ STYLE LOCK:
- имитируй стиль исходных сохраненных карточек, не улучшай и не меняй художественную систему;
- круглая область кожи, тонкая темная обводка, мягкие красно-розовые градиенты, насекомое у края круга;
- тот же масштаб, та же плотность деталей, та же простая медицинская иллюстрация;
- НЕ делать фотореализм, 3D, глянец, макро-фото, мультяшный стиль или новый дизайн;
- НЕ добавлять текст, буквы, подписи, watermark, фон страницы, интерфейс;
- фон за пределами объекта должен быть прозрачным или максимально чистым для последующего удаления.

Безопасность и медицинский тон:
- нейтральная образовательная медицинская иконка;
- без крови, некроза, открытых ран, пугающих деталей;
- без людей, без интимных частей тела, без наготы.
""".strip()




def _is_moderation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "moderation_blocked" in text or "safety" in text or "safety system" in text or "safety_violations" in text


def build_safe_semantic_png_prompt(task: dict[str, Any], payload: dict[str, Any]) -> str:
    """Short, safety-friendly fallback prompt for medical icon generation.

    The first v41 test showed that long dermatology prompts can be falsely rejected by
    the image safety layer. This prompt intentionally avoids old source text,
    body-part lists, diagnosis language and scary wound terms.
    """
    png_id = str(task.get("png_id") or "semantic_png")
    entity_id = str(task.get("entity_id") or "")
    include = ", ".join(str(x) for x in task.get("must_include", [])[:5])
    exclude = ", ".join(str(x) for x in task.get("must_exclude", [])[:5])
    return f"""
Create one neutral non-sexual educational medical illustration icon for a social media infographic.
It must show only a small circular dermatology-style patch and a simple insect or arthropod icon.
No full human body, no intimate body parts, no nudity, no person, no blood, no gore, no open wound, no text.

ID: {png_id} {entity_id}
Main visual elements to include: {include or 'round medical reaction patch and small insect icon'}.
Elements to avoid: {exclude or 'old labels, interface buttons, watermark, rectangular background'}.

Style: clean vector-like medical illustration, soft gradients, transparent-looking background, calm clinic design, 1024x1024 square.
""".strip()


def _create_fallback_semantic_png(task: dict[str, Any], path: Path, reason: str = "") -> None:
    """Create a local placeholder PNG so one blocked image does not stop the whole pipeline."""
    png_id = str(task.get("png_id") or path.stem)
    entity_id = str(task.get("entity_id") or "")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")


def generate_semantic_pngs(asset_id: int, limit: int | None = None) -> tuple[list[str], list[str]]:
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
    fallbacks: list[dict[str, str]] = []
    cost_items: list[dict[str, Any]] = []
    client = _client()
    source_image = _source_image_for_asset(asset_id)

    count = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        png_id = str(task.get("png_id") or f"png_{count+1:03d}")
        output_name = _safe_name(task.get("output_name") or png_id, png_id)
        path = out_dir / f"{output_name}.png"
        if path.exists() and path.stat().st_size > 0:
            _register_artifact_safe(asset_id, state_id, "semantic_png", path)
            skipped.append(str(path))
            continue
        if _restore_semantic_png_from_registry(asset_id, state_id, output_name, path):
            skipped.append(str(path))
            continue
        if limit is not None and count >= limit:
            break

        operation = str(task.get("operation") or "").lower()
        quality_strategy = str(task.get("quality_strategy") or "").lower()
        if operation == "extract_from_source" and quality_strategy in {"preserve_original_resolution", "extract_no_upscale", ""}:
            if _extract_source_png(task, source_image, path):
                _register_artifact_safe(asset_id, state_id, "semantic_png", path)
                done.append(str(path))
                cost_items.append(free_operation(
                    "semantic_png_extract",
                    {"asset_id": asset_id, "png_id": png_id, "path": str(path), "quality_strategy": quality_strategy},
                ))
                count += 1
                continue

        prompts = [
            build_semantic_png_prompt(task, payload),
            build_safe_semantic_png_prompt(task, payload),
        ]
        last_exc: Exception | None = None
        for prompt in prompts:
            try:
                response = client.images.generate(
                    model=settings.openai_image_model,
                    prompt=prompt,
                    size="1024x1024",
                    n=1,
                )
                image_data = response.data[0]
                b64_json = getattr(image_data, "b64_json", None)
                if not b64_json:
                    raise SemanticAssetError("OpenAI Images API не вернул b64_json.")
                path.write_bytes(base64.b64decode(b64_json))
                _register_artifact_safe(asset_id, state_id, "semantic_png", path)
                done.append(str(path))
                cost_items.append(cost_for_image_generation(
                    operation="semantic_png_generate",
                    model=settings.openai_image_model,
                    image_count=1,
                    size="1024x1024",
                    metadata={"asset_id": asset_id, "png_id": png_id, "path": str(path), "quality_strategy": quality_strategy},
                ))
                count += 1
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                # If safety rejected a long prompt, immediately try the short safe prompt.
                # For other transient errors, the second prompt is still a useful retry.
                continue

        if last_exc is not None:
            # Do not stop the entire v41 pipeline because one semantic image was blocked.
            _create_fallback_semantic_png(task, path, str(last_exc))
            _register_artifact_safe(asset_id, state_id, "semantic_png", path)
            done.append(str(path))
            fallbacks.append({"png_id": png_id, "path": str(path), "reason": str(last_exc)[:500]})
            cost_items.append(free_operation(
                "semantic_png_fallback",
                {"asset_id": asset_id, "png_id": png_id, "path": str(path), "reason": str(last_exc)[:500]},
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
        "fallbacks": fallbacks,
        "cost_estimate": cost_summary,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _register_artifact_safe(asset_id, state_id, "manifest", manifest_path)
    return done, skipped, cost_summary

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

    # Header area: text is fitted into the available block instead of being
    # drawn with a fixed font that can overflow on long Russian headings.
    header_x = 60
    header_y = 26
    header_w = width - 120
    header_h = 138
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

    cards = pack.get("cards") if isinstance(pack.get("cards"), list) else []
    cards = [c for c in cards if isinstance(c, dict)]
    pngs = _png_lookup(asset_id, state_id, payload)

    layout_text = str(bp.get("layout") or "")
    cols, rows = _infer_grid(len(cards), layout_text)
    cols = max(1, cols)
    rows = max(1, rows)

    margin_x = 50
    gap_x = 24 if cols <= 2 else 18
    gap_y = 18 if rows <= 5 else 12
    footer_h = 155
    footer_bottom = 45
    start_y = 188
    footer_y = height - footer_bottom - footer_h
    available_h = max(260, footer_y - start_y - 18)
    card_w = (width - margin_x * 2 - gap_x * (cols - 1)) // cols
    card_h = max(130, (available_h - gap_y * (rows - 1)) // rows)

    # If the inferred grid cannot fit all cards, extend rows rather than
    # silently dropping cards. The old Composer truncated cards[:10].
    max_cards = cols * rows
    if len(cards) > max_cards:
        rows = math.ceil(len(cards) / cols)
        card_h = max(110, (available_h - gap_y * (rows - 1)) // rows)

    for idx, card in enumerate(cards):
        col = idx % cols
        row = idx // cols
        if row >= rows:
            break
        x = margin_x + col * (card_w + gap_x)
        cy = start_y + row * (card_h + gap_y)
        draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=22, fill="#FFFFFF", outline="#F7D8C8", width=2)

        png_id = str(card.get("png_id") or "")
        png_path = pngs.get(png_id)

        if cols <= 2:
            icon_box = min(132, max(78, card_h - 32), card_w // 3)
            icon_x = x + 18
            icon_y = cy + max(12, (card_h - icon_box) // 2)
            text_x = x + icon_box + 38
            text_y = cy + 22
            text_w = max(90, card_w - icon_box - 58)
            title_h = max(28, int(card_h * 0.28))
            body_h = max(34, card_h - title_h - 48)
            title_max, title_min = 28, 16
            body_max, body_min = 22, 13
        else:
            icon_box = min(max(70, int(card_h * 0.43)), card_w - 28)
            icon_x = x + (card_w - icon_box) // 2
            icon_y = cy + 12
            text_x = x + 14
            text_y = icon_y + icon_box + 8
            text_w = card_w - 28
            remaining_h = max(40, card_h - (text_y - cy) - 12)
            title_h = max(22, int(remaining_h * 0.36))
            body_h = max(22, remaining_h - title_h)
            title_max, title_min = 19, 11
            body_max, body_min = 14, 9

        if png_path and png_path.exists():
            try:
                p_img = Image.open(png_path).convert("RGBA")
                p_img.thumbnail((icon_box, icon_box), Image.Resampling.LANCZOS)
                px = icon_x + (icon_box - p_img.width) // 2
                py = icon_y + (icon_box - p_img.height) // 2
                img.paste(p_img, (px, py), p_img)
            except Exception:
                draw.ellipse((icon_x, icon_y, icon_x + icon_box, icon_y + icon_box), fill="#F7D8C8", outline="#F2B8A8")
        else:
            draw.ellipse((icon_x, icon_y, icon_x + icon_box, icon_y + icon_box), fill="#F7D8C8", outline="#F2B8A8")

        title = str(card.get("title") or card.get("card_id") or "")
        short_text = str(card.get("short_text") or "")
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
        _draw_fitted_text(
            draw,
            (text_x, body_y),
            short_text,
            text_w,
            max(16, cy + card_h - body_y - 12),
            max_size=body_max,
            min_size=body_min,
            fill="#444444",
            bold=False,
            max_lines=3 if cols <= 2 else 4,
        )

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
