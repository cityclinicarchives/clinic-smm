from __future__ import annotations

import base64
import json
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


def _remove_flat_corner_background(crop: Image.Image) -> Image.Image:
    """Remove only border-connected flat background while preserving native resolution.

    Older implementation turned every beige pixel transparent and could damage skin
    gradients. This version flood-fills from image borders using corner background
    samples, so the inner dermatology circle is preserved even when skin colors are
    close to the page background.
    """
    img = crop.convert('RGBA')
    pixels = img.load()
    w, h = img.size
    if w < 2 or h < 2:
        return img
    samples = [pixels[0, 0], pixels[w - 1, 0], pixels[0, h - 1], pixels[w - 1, h - 1]]
    bg = tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))
    threshold = 42
    seen: set[tuple[int, int]] = set()
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        q.append((x, 0)); q.append((x, h - 1))
    for y in range(h):
        q.append((0, y)); q.append((w - 1, y))
    while q:
        x, y = q.popleft()
        if (x, y) in seen or x < 0 or y < 0 or x >= w or y >= h:
            continue
        r, g, b, a = pixels[x, y]
        if a == 0 or _color_distance((r, g, b), bg) <= threshold:
            seen.add((x, y))
            pixels[x, y] = (r, g, b, 0)
            q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return img


def _remove_text_like_edge_fragments(img: Image.Image) -> Image.Image:
    """Remove small disconnected dark components near crop edges.

    This is a cheap guard against old labels entering the crop. It does not use OCR;
    it removes only small text-like components near the top/bottom/side edges while
    keeping the large circular visual and the insect/chlenistonogoe object.
    """
    img = img.convert('RGBA')
    pixels = img.load()
    w, h = img.size
    alpha_seen: set[tuple[int, int]] = set()
    edge_band_y = max(24, int(h * 0.18))
    edge_band_x = max(24, int(w * 0.12))

    def is_opaque(px: int, py: int) -> bool:
        return pixels[px, py][3] > 18

    for sy in range(h):
        for sx in range(w):
            if (sx, sy) in alpha_seen or not is_opaque(sx, sy):
                continue
            q: deque[tuple[int, int]] = deque([(sx, sy)])
            comp: list[tuple[int, int]] = []
            alpha_seen.add((sx, sy))
            minx = maxx = sx
            miny = maxy = sy
            dark_count = 0
            while q:
                x, y = q.popleft()
                comp.append((x, y))
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                r, g, b, a = pixels[x, y]
                if a > 18 and r + g + b < 260:
                    dark_count += 1
                for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in alpha_seen and is_opaque(nx, ny):
                        alpha_seen.add((nx, ny))
                        q.append((nx, ny))
            area = len(comp)
            bw, bh = maxx - minx + 1, maxy - miny + 1
            touches_edge_band = miny < edge_band_y or maxy > h - edge_band_y or minx < edge_band_x or maxx > w - edge_band_x
            text_like = (area < max(260, (w * h) // 900) and bh <= max(34, int(h * 0.13)) and bw <= max(90, int(w * 0.35)))
            mostly_dark = dark_count / max(1, area) > 0.45
            # Remove old label letters and interface fragments, but avoid deleting large insects or the circle.
            if touches_edge_band and text_like and mostly_dark:
                for x, y in comp:
                    r, g, b, a = pixels[x, y]
                    pixels[x, y] = (r, g, b, 0)
    return img


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
    path = find_analysis_file(asset_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_analysis_path"] = str(path)
    return data


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

    out_dir = semantic_png_dir(asset_id, _state_id(data))
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
            skipped.append(str(path))
            continue
        if limit is not None and count >= limit:
            break

        operation = str(task.get("operation") or "").lower()
        quality_strategy = str(task.get("quality_strategy") or "").lower()
        if operation == "extract_from_source" and quality_strategy in {"preserve_original_resolution", "extract_no_upscale", ""}:
            if _extract_source_png(task, source_image, path):
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
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return done, skipped, cost_summary

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        width = draw.textbbox((0, 0), test, font=font)[2]
        if width <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


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
    return lookup


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
    title_font = _font(58, bold=True)
    subtitle_font = _font(28)
    card_title_font = _font(28, bold=True)
    card_text_font = _font(22)
    footer_title_font = _font(30, bold=True)
    footer_text_font = _font(24)

    pack = _content_pack(payload)
    header = pack.get("header") if isinstance(pack.get("header"), dict) else {}
    header_text = header.get("text") or bp.get("header", {}).get("text") or payload.get("analysis_state", {}).get("topic") or "Медицинская памятка"
    subtitle = "Это ориентиры, не диагноз. Важны симптомы и обстоятельства"
    if isinstance(header.get("subtitle"), str) and header.get("subtitle"):
        subtitle = header["subtitle"]

    y = 28
    for line in _wrap(draw, str(header_text), title_font, width - 120)[:2]:
        draw.text((60, y), line, fill="#1E1E1E", font=title_font)
        y += 66
    for line in _wrap(draw, subtitle, subtitle_font, width - 120)[:2]:
        draw.text((60, y + 5), line, fill="#2F6F5E", font=subtitle_font)
        y += 36

    cards = pack.get("cards") if isinstance(pack.get("cards"), list) else []
    cards = cards[:10]
    pngs = _png_lookup(asset_id, state_id, payload)

    margin_x = 50
    gap_x = 24
    gap_y = 18
    cols = 2 if len(cards) > 9 else 3
    rows = 5 if cols == 2 else 3
    card_w = (width - margin_x * 2 - gap_x * (cols - 1)) // cols
    card_h = 166 if cols == 2 else 275
    start_y = 205 if cols == 2 else 220

    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        col = idx % cols
        row = idx // cols
        x = margin_x + col * (card_w + gap_x)
        cy = start_y + row * (card_h + gap_y)
        draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=24, fill="#FFFFFF", outline="#F7D8C8", width=2)
        png_id = str(card.get("png_id") or "")
        png_path = pngs.get(png_id)
        icon_box = 126 if cols == 2 else 150
        if png_path and png_path.exists():
            try:
                p = Image.open(png_path).convert("RGBA")
                p.thumbnail((icon_box, icon_box))
                img.paste(p, (x + 16, cy + (card_h - p.height) // 2), p if p.mode == "RGBA" else None)
            except Exception:
                pass
        else:
            draw.ellipse((x + 24, cy + 22, x + 124, cy + 122), fill="#F7D8C8", outline="#F2B8A8")

        title = str(card.get("title") or card.get("card_id") or "")
        text_x = x + 158 if cols == 2 else x + 18
        text_w = card_w - 176 if cols == 2 else card_w - 36
        ty = cy + 24 if cols == 2 else cy + 168
        for line in _wrap(draw, title, card_title_font, text_w)[:2]:
            draw.text((text_x, ty), line, fill="#1E1E1E", font=card_title_font)
            ty += 32
        text = str(card.get("short_text") or "")
        for line in _wrap(draw, text, card_text_font, text_w)[:3]:
            draw.text((text_x, ty + 4), line, fill="#444444", font=card_text_font)
            ty += 26

    footer_y = height - 185
    draw.rounded_rectangle((50, footer_y, width - 50, height - 45), radius=28, fill="#FFE7E3")
    footer_blocks = pack.get("footer_blocks") if isinstance(pack.get("footer_blocks"), list) else []
    if footer_blocks and isinstance(footer_blocks[0], dict):
        ftitle = footer_blocks[0].get("title") or "Срочно за помощью"
        ftext = footer_blocks[0].get("text") or "Одышка, отек лица или горла, слабость, быстро растущее покраснение, гной, лихорадка."
    else:
        ftitle = "Срочно за помощью"
        ftext = "Одышка, отек лица или горла, слабость, быстро растущее покраснение, гной, лихорадка."
    draw.text((82, footer_y + 26), "!", fill="#C83F3F", font=_font(48, bold=True))
    draw.text((128, footer_y + 28), str(ftitle), fill="#C83F3F", font=footer_title_font)
    ty = footer_y + 72
    for line in _wrap(draw, str(ftext), footer_text_font, width - 180)[:3]:
        draw.text((128, ty), line, fill="#1E1E1E", font=footer_text_font)
        ty += 30

    out = reconstruction_dir() / f"asset-{asset_id}-state-{state_id or 'latest'}-reconstruction.png"
    img.save(out, "PNG")
    return str(out)
