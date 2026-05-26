import base64
import json
import math
import re
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from app.config import settings
from app.models import ContentAsset, ContentPost, ContentReconstruction
from app.services.telegram_bot import download_file_bytes
from app.services.blueprint_layout import choose_format, auto_plan_layout, validate_ai_layout, validate_blueprint, final_qa
from app.services.atomic_blueprint import atomic_blueprint_issues, normalize_atomic_blocks, repair_atomic_blueprint_with_ai
from app.services.reconstruction_contract import validate_contract_on_spec, contract_critical_issues, contract_summary


class ComponentInfographicError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise ComponentInfographicError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-zа-яё0-9]+", "-", value, flags=re.IGNORECASE)
    value = value.strip("-")
    return value[:60] or "component-infographic"


def _load_spec(reconstruction: ContentReconstruction) -> dict[str, Any]:
    try:
        return json.loads(reconstruction.reconstruction_spec or "{}")
    except Exception:
        return {}


def has_component_reference(asset: ContentAsset | None) -> bool:
    return bool(asset and asset.media_file_id and asset.media_type in {"photo", "document"})


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/opt/pyvenv/lib/python3.13/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
            "/opt/venv/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/opt/pyvenv/lib/python3.13/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf",
        "/opt/venv/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_round_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: str, outline: str | None = None, width: int = 1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int | None = None) -> list[str]:
    text = str(text or "").replace("\n", " ").strip()
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        try:
            width = draw.textbbox((0, 0), candidate, font=font)[2]
        except Exception:
            width = len(candidate) * 10
        if width <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if max_lines and len(lines) >= max_lines:
                break
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines


def _draw_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.ImageFont,
    fill: str,
    max_width: int,
    line_gap: int = 8,
    max_lines: int | None = None,
) -> int:
    x, y = xy
    lines = _wrap_text(draw, text, font, max_width, max_lines=max_lines)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        try:
            h = draw.textbbox((x, y), line, font=font)[3] - draw.textbbox((x, y), line, font=font)[1]
        except Exception:
            h = 24
        y += h + line_gap
    return y


def _normalize_bbox(raw: Any, image_w: int, image_h: int) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        x = raw.get("x") or raw.get("left") or 0
        y = raw.get("y") or raw.get("top") or 0
        w = raw.get("w") or raw.get("width")
        h = raw.get("h") or raw.get("height")
        if w is None or h is None:
            x2 = raw.get("x2") or raw.get("right")
            y2 = raw.get("y2") or raw.get("bottom")
            if x2 is None or y2 is None:
                return None
            w = float(x2) - float(x)
            h = float(y2) - float(y)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        x, y, w, h = raw[:4]
    else:
        return None
    try:
        x = float(x); y = float(y); w = float(w); h = float(h)
    except Exception:
        return None
    # Normalized coordinates are preferred.
    if max(abs(x), abs(y), abs(w), abs(h)) <= 1.5:
        x1 = int(x * image_w)
        y1 = int(y * image_h)
        x2 = int((x + w) * image_w)
        y2 = int((y + h) * image_h)
    else:
        x1 = int(x); y1 = int(y); x2 = int(x + w); y2 = int(y + h)
    x1 = max(0, min(image_w - 1, x1)); y1 = max(0, min(image_h - 1, y1))
    x2 = max(x1 + 1, min(image_w, x2)); y2 = max(y1 + 1, min(image_h, y2))
    if (x2 - x1) < 10 or (y2 - y1) < 10:
        return None
    return (x1, y1, x2, y2)




def _bbox_area_ratio(raw: Any) -> float | None:
    if not isinstance(raw, dict):
        return None
    try:
        w = float(raw.get("w", raw.get("width")))
        h = float(raw.get("h", raw.get("height")))
    except Exception:
        return None
    if w > 1.5 or h > 1.5:
        return None
    return max(0.0, w) * max(0.0, h)

def _infer_bbox_from_hint(hint: str | None, image_w: int, image_h: int) -> tuple[int, int, int, int] | None:
    """Fallback for common grid infographics when AI did not provide bbox."""
    hint = (hint or "").lower()
    m = re.search(r"row\s*(\d+)\s*col\s*(\d+)", hint)
    if not m:
        m = re.search(r"ряд\s*(\d+)\s*(?:колонка|столбец)\s*(\d+)", hint)
    if not m:
        return None
    row = int(m.group(1)); col = int(m.group(2))
    # Assume header top 14%, footer bottom 8%, grid 3 columns.
    top = int(image_h * 0.14)
    bottom = int(image_h * 0.86)
    cols = 3
    rows = max(3, row)
    cell_w = image_w / cols
    cell_h = (bottom - top) / rows
    pad_x = int(cell_w * 0.08)
    pad_y = int(cell_h * 0.08)
    x1 = int((col - 1) * cell_w + pad_x)
    y1 = int(top + (row - 1) * cell_h + pad_y)
    x2 = int(col * cell_w - pad_x)
    y2 = int(top + row * cell_h - pad_y)
    return (max(0, x1), max(0, y1), min(image_w, x2), min(image_h, y2))



def _semantic_trim_visual_crop(crop: Image.Image, block: dict[str, Any]) -> Image.Image:
    """Trim a source crop to the useful visual component.

    This is a lightweight local crop-cleaner before optional AI cleanup. It is
    designed for infographic cards where the useful elements are a photo/lesion
    and a relevant object, while labels, colored backgrounds, screenshots and UI
    should be excluded. It does not use OCR: it relies on color/shape masks and
    conservative margins, so it is cheap and deterministic.
    """
    img = ImageOps.exif_transpose(crop).convert("RGB")
    w, h = img.size
    if w < 20 or h < 20:
        return img
    try:
        import numpy as np
    except Exception:
        return img

    arr = np.asarray(img)
    r = arr[:, :, 0].astype("int16")
    g = arr[:, :, 1].astype("int16")
    b = arr[:, :, 2].astype("int16")

    # Skin/lesion-like regions: pink/beige/red zones with enough saturation.
    skin = (
        (r > 115) & (g > 65) & (b > 45) &
        (r >= g - 10) & (r >= b + 10) &
        ~((r > 225) & (g > 225) & (b > 225))
    )
    # Dark objects near the lesion are often the insect/arthropod.
    dark = (r < 115) & (g < 115) & (b < 115)
    # Exclude common source labels/backgrounds from the crop decision.
    yellow_label = (r > 170) & (g > 135) & (b < 95)
    blue_bg = (b > 110) & (g > 115) & (r < 120)
    ui_white = (r > 235) & (g > 235) & (b > 235)

    useful = skin & ~yellow_label & ~blue_bg & ~ui_white
    ys, xs = np.where(useful)
    if len(xs) < max(40, (w * h) // 250):
        return img

    sx1, sx2 = int(xs.min()), int(xs.max())
    sy1, sy2 = int(ys.min()), int(ys.max())
    # Include nearby dark insect/object pixels, but not far-away UI text.
    pad_x = max(12, int((sx2 - sx1 + 1) * 0.55))
    pad_y = max(12, int((sy2 - sy1 + 1) * 0.55))
    rx1, ry1 = max(0, sx1 - pad_x), max(0, sy1 - pad_y)
    rx2, ry2 = min(w - 1, sx2 + pad_x), min(h - 1, sy2 + pad_y)
    roi_dark = dark[ry1:ry2 + 1, rx1:rx2 + 1] & ~blue_bg[ry1:ry2 + 1, rx1:rx2 + 1] & ~yellow_label[ry1:ry2 + 1, rx1:rx2 + 1]
    dys, dxs = np.where(roi_dark)
    if len(dxs) > 10:
        dxs = dxs + rx1
        dys = dys + ry1
        ux1 = min(sx1, int(dxs.min()))
        ux2 = max(sx2, int(dxs.max()))
        uy1 = min(sy1, int(dys.min()))
        uy2 = max(sy2, int(dys.max()))
    else:
        ux1, ux2, uy1, uy2 = sx1, sx2, sy1, sy2

    # Final margin for aesthetics, but keep it tight enough to remove labels/UI.
    bw = ux2 - ux1 + 1
    bh = uy2 - uy1 + 1
    margin = max(10, int(max(bw, bh) * 0.18))
    x1 = max(0, ux1 - margin)
    y1 = max(0, uy1 - margin)
    x2 = min(w, ux2 + margin)
    y2 = min(h, uy2 + margin)

    # Avoid returning a huge chunk of the original screenshot. If trimming did
    # not reduce the crop enough, still remove top/bottom source label bands.
    if (x2 - x1) * (y2 - y1) > w * h * 0.82:
        # Heuristic: keep central useful region around the skin bbox.
        x1 = max(0, sx1 - pad_x)
        y1 = max(0, sy1 - pad_y)
        x2 = min(w, sx2 + pad_x)
        y2 = min(h, sy2 + pad_y)
    if (x2 - x1) < 15 or (y2 - y1) < 15:
        return img
    return img.crop((x1, y1, x2, y2))


def _crop_from_source(source: Image.Image, block: dict[str, Any]) -> Image.Image | None:
    # v34: AI provides bbox and crop intent; Python performs semantic trimming
    # so old labels/background/UI do not get passed further as visual content.
    area = _bbox_area_ratio(block.get("source_bbox"))
    block_type = str(block.get("type") or "").lower()
    if block_type in {"comparison_card", "card", "tile", "visual_card", "comparison_item"} and area is not None and area > 0.22:
        return None
    bbox = _normalize_bbox(block.get("source_bbox"), source.width, source.height)
    if not bbox:
        bbox = _infer_bbox_from_hint(block.get("source_location_hint"), source.width, source.height)
    if not bbox:
        return None
    crop = source.crop(bbox).convert("RGB")
    crop = ImageOps.exif_transpose(crop)
    policy = str(block.get("source_policy") or "").lower()
    crop_intent = block.get("crop_intent") if isinstance(block.get("crop_intent"), dict) else {}
    crop_mode = str(block.get("crop_mode") or crop_intent.get("mode") or "").lower()
    if policy == "use_reference_and_clean" or "clean" in crop_mode or "object" in crop_mode:
        crop = _semantic_trim_visual_crop(crop, block)
    return crop


def _crop_quality_issues(crop: Image.Image | None, block: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if crop is None:
        return ["crop_missing"]
    w, h = crop.size
    if w < 50 or h < 50:
        issues.append("crop_too_small_after_semantic_trim")
    ratio = w / max(h, 1)
    if ratio < 0.35 or ratio > 2.8:
        issues.append("crop_extreme_aspect_ratio_possible_bad_bbox")
    # A cleaned visual component should not be a tall phone screenshot strip.
    if h > w * 2.2 and str(block.get("source_policy") or "").lower() == "use_reference_and_clean":
        issues.append("clean_crop_still_looks_like_screenshot_strip")
    return issues


def _generate_replacement_visual(block: dict[str, Any], style: str, size: int = 512) -> Image.Image | None:
    prompt = f"""
Create a clean medical illustration WITHOUT any text.
Subject: {block.get('visual_element') or block.get('title')}
Style: {style or 'clean modern medical infographic illustration'}.
Requirements: no letters, no words, no labels, no watermark, centered object, light background, clear medical visual, suitable for a small card in a Russian clinic infographic.
""".strip()
    try:
        client = _get_client()
        response = client.images.generate(
            model=settings.openai_image_model,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        b64_json = getattr(response.data[0], "b64_json", None)
        if not b64_json:
            return None
        img = Image.open(BytesIO(base64.b64decode(b64_json))).convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        return img
    except Exception:
        return None




def _clean_reference_crop_with_ai(crop: Image.Image, block: dict[str, Any], style: str, size: int = 768) -> Image.Image | None:
    """Use image AI to clean a reference crop into a reusable visual component.

    The source crop often contains labels, blue background, watermark fragments,
    or social UI. For component cards we need only the useful visual: e.g. bite
    circle + insect, without duplicated text labels.
    """
    try:
        client = _get_client()
        with NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            tmp_path = Path(tmp.name)
            crop.convert('RGB').save(tmp_path, format='PNG')
        title = str(block.get('title') or block.get('new_element') or block.get('visual_element') or 'visual element')
        prompt = f"""
You are preparing one clean visual component for a medical infographic card.
Use the attached crop ONLY as visual reference.

Goal: create a clean isolated visual for the card: {title}.

Keep from reference only the medically useful visual elements:
- the bite/skin reaction circle or lesion example;
- the insect/arthropod illustration if present and relevant.

Remove completely:
- all text labels and captions;
- yellow labels;
- blue source background;
- social network UI, likes, icons, username, watermark;
- duplicated titles, logos, borders, screenshots.

Style requirements:
- no words, no letters, no numbers, no captions;
- clean light medical background;
- realistic/clear bite circle and insect in the same visual style as the source;
- centered composition with enough margin;
- suitable for placement inside a modern Russian medical infographic card;
- do not change the medical meaning of the bite example.

If the source crop contains the wrong old element that must be replaced, ignore it and create the new element described here:
old_element={block.get('old_element') or ''}
new_element={block.get('new_element') or ''}
replacement_prompt={block.get('replacement_prompt') or ''}
""".strip()
        with open(tmp_path, 'rb') as image_file:
            response = client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
                size='1024x1024',
                n=1,
            )
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        b64_json = getattr(response.data[0], 'b64_json', None)
        if not b64_json:
            return None
        img = Image.open(BytesIO(base64.b64decode(b64_json))).convert('RGB')
        img.thumbnail((size, size), Image.LANCZOS)
        return img
    except Exception:
        return None


def _fit_font_to_width(text: str, max_width: int, start_size: int, min_size: int = 16, bold: bool = True):
    text = str(text or '')
    probe = Image.new('RGB', (10, 10))
    d = ImageDraw.Draw(probe)
    for size in range(start_size, min_size - 1, -2):
        font = _find_font(size, bold=bold)
        try:
            width = d.textbbox((0, 0), text, font=font)[2]
        except Exception:
            width = len(text) * size * 0.5
        if width <= max_width:
            return font
    return _find_font(min_size, bold=bold)


def _prepare_visual(source: Image.Image, block: dict[str, Any], style: str, target_w: int, target_h: int) -> Image.Image:
    policy = str(block.get("source_policy") or "generate_new").lower()
    cleanup_mode = bool(block.get("visual_cleanup") or block.get("remove_source_labels") or policy == "use_reference_and_clean")
    visual: Image.Image | None = None

    if policy in {"preserve_from_reference", "use_reference_and_clean"}:
        raw_crop = _crop_from_source(source, block)
        if raw_crop is not None and cleanup_mode:
            visual = _clean_reference_crop_with_ai(raw_crop, block, style=style, size=max(target_w, target_h)) or raw_crop
        else:
            visual = raw_crop

    if visual is None and policy in {"replace_with_new", "generate_new"}:
        visual = _generate_replacement_visual(block, style=style, size=max(target_w, target_h))

    if visual is None:
        # Soft placeholder, not blank, so the card remains usable.
        visual = Image.new("RGB", (target_w, target_h), "#EAF6F6")
        d = ImageDraw.Draw(visual)
        font = _find_font(32, bold=True)
        text = str(block.get("title") or block.get("visual_element") or "")[:18]
        _draw_text_block(d, text, (20, target_h // 2 - 30), font, "#17435F", target_w - 40, max_lines=2)

    visual = ImageOps.exif_transpose(visual).convert("RGB")
    # For cleaned components use contain mode here; later _fit_image_cover may cover-crop if needed.
    visual.thumbnail((target_w, target_h), Image.LANCZOS)
    bg = Image.new("RGB", (target_w, target_h), "#FFFFFF")
    x = (target_w - visual.width) // 2
    y = (target_h - visual.height) // 2
    bg.paste(visual, (x, y))
    return bg


def _card(canvas: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], block: dict[str, Any], visual: Image.Image | None = None, accent: str = "#1B928F"):
    x1, y1, x2, y2 = box
    w = x2 - x1; h = y2 - y1
    _draw_round_rect(draw, box, radius=22, fill="#FFFFFF", outline="#B8D6D9", width=2)
    title_font = _find_font(max(28, int(h * 0.12)), bold=True)
    text_font = _find_font(max(20, int(h * 0.065)), bold=False)
    num_font = _find_font(max(24, int(h * 0.10)), bold=True)
    num = str(block.get("number") or "")
    tx = x1 + 24
    ty = y1 + 20
    if num:
        draw.ellipse((tx, ty, tx + 42, ty + 42), fill=accent)
        draw.text((tx + 13, ty + 7), num, font=num_font, fill="#FFFFFF")
        title_x = tx + 55
    else:
        title_x = tx
    _draw_text_block(draw, str(block.get("title") or ""), (title_x, ty), title_font, "#12304A", x2 - title_x - 16, max_lines=2)

    visual_h = int(h * 0.42)
    visual_w = int(w * 0.86)
    if visual is not None:
        visual.thumbnail((visual_w, visual_h), Image.LANCZOS)
        vx = x1 + (w - visual.width) // 2
        vy = y1 + int(h * 0.22)
        canvas.paste(visual, (vx, vy))

    lines = block.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    y = y1 + int(h * 0.68)
    for line in lines[:3]:
        line = str(line).strip()
        if not line:
            continue
        draw.text((x1 + 30, y), "•", font=text_font, fill=accent)
        y = _draw_text_block(draw, line, (x1 + 55, y), text_font, "#16334A", w - 80, line_gap=5, max_lines=2)



def _info_box(canvas: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, items: list[str], accent: str, icon: str = "!"):
    x1, y1, x2, y2 = box
    _draw_round_rect(draw, box, radius=22, fill="#FFFFFF", outline="#C8DEE0", width=2)
    head_h = min(68, max(54, int((y2 - y1) * 0.25)))
    _draw_round_rect(draw, (x1, y1, x2, y1 + head_h), radius=22, fill=accent, outline=None)
    title_font = _fit_font_to_width(title, max(80, x2 - x1 - 86), 30, min_size=16, bold=True)
    text_font = _find_font(21 if (y2-y1) < 260 else 23, bold=False)
    draw.text((x1 + 22, y1 + 13), icon, font=title_font, fill="#FFFFFF")
    _draw_text_block(draw, title, (x1 + 64, y1 + 11), title_font, "#FFFFFF", x2 - x1 - 86, line_gap=2, max_lines=1)
    y = y1 + head_h + 14
    bottom_limit = y2 - 18
    for item in items[:6]:
        if y > bottom_limit - 28:
            break
        draw.text((x1 + 26, y), "•", font=text_font, fill=accent)
        before = y
        # Use a single line when the block is tight, two lines only if there is room.
        remaining = bottom_limit - y
        max_lines = 2 if remaining > 60 else 1
        y = _draw_text_block(draw, item, (x1 + 52, y), text_font, "#143047", x2 - x1 - 78, line_gap=5, max_lines=max_lines)
        if y <= before:
            y += 24


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [str(value)] if str(value).strip() else []


def _extract_blocks(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks = spec.get("structure", {}).get("blocks") or []
    if not isinstance(blocks, list):
        blocks = []
    content_cards: list[dict[str, Any]] = []
    meta_blocks: list[dict[str, Any]] = []
    footer_blocks: list[dict[str, Any]] = []
    for i, b in enumerate(blocks, start=1):
        if not isinstance(b, dict):
            continue
        t = str(b.get("type") or "").lower()
        if t in {"comparison_card", "card", "tile", "visual_card"}:
            b = dict(b)
            b.setdefault("number", len(content_cards) + 1)
            content_cards.append(b)
        elif t in {"warning_block", "action_block", "footer", "disclaimer", "header"}:
            meta_blocks.append(b)
        else:
            # If it has visual source policy, treat as content card.
            if b.get("source_policy") in {"preserve_from_reference", "use_reference_and_clean", "replace_with_new", "generate_new"}:
                b = dict(b); b.setdefault("number", len(content_cards) + 1); content_cards.append(b)
            else:
                meta_blocks.append(b)
    # Use AI-declared meta blocks too. v25 ignored most meta_blocks; v26 renders them.
    for mb in meta_blocks:
        t = str(mb.get("type") or "").lower()
        if t == "header":
            continue
        if t in {"warning_block", "action_block", "footer", "disclaimer"}:
            footer_blocks.append(mb)

    # Add warning/action blocks if they exist separately.
    structure = spec.get("structure") or {}
    if structure.get("warning_block"):
        footer_blocks.append({"type": "warning_block", "title": "Срочно к врачу, если:", "lines": _as_list(structure.get("warning_block"))})
    if structure.get("action_block"):
        footer_blocks.append({"type": "action_block", "title": "Что делать после укуса:", "lines": _as_list(structure.get("action_block"))})
    if structure.get("footer"):
        footer_blocks.append({"type": "footer", "title": "Важно", "lines": _as_list(structure.get("footer"))})

    # Deduplicate footer blocks by title/type to avoid repeating the same warning twice.
    deduped = []
    seen = set()
    for fb in footer_blocks:
        key = (str(fb.get("type") or "").lower(), str(fb.get("title") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fb)
    return content_cards, meta_blocks, deduped




def _is_critical_issue(issue: str) -> bool:
    critical_patterns = [
        "no_atomic_visual_cards",
        "no_structure_blocks",
        "reference_policy_without_bbox",
        "bbox_too_large",
        "bbox_too_small",
        "is_grouped_not_atomic",
        "source_suggests_9_cards_but_only",
        "has_no_layout_and_was_not_rendered",
        "rendered_cards=",
        "outside_canvas",
        "crop_semantic_quality_failed",
        "clean_crop_still_looks_like_screenshot_strip",
        "crop_extreme_aspect_ratio_possible_bad_bbox",
    ]
    return any(pattern in str(issue) for pattern in critical_patterns)


def _critical_issues(issues: list[str]) -> list[str]:
    return [issue for issue in issues if _is_critical_issue(issue)]


def analyze_component_infographic_blueprint(
    reconstruction: ContentReconstruction,
    asset: ContentAsset | None,
) -> dict[str, Any]:
    """Return a machine-readable diagnostic report before final rendering.

    This is intentionally conservative. If the blueprint is not atomic enough or
    crops cannot be extracted safely, the final render should be stopped until
    reconstruction/blueprint is repaired.
    """
    if not has_component_reference(asset):
        return {
            "ok": False,
            "critical_issues": ["no_reference_image"],
            "issues": ["У исходника нет изображения для crop-preview."],
            "cards": [],
            "footer_blocks": [],
        }

    spec = normalize_atomic_blocks(_load_spec(reconstruction))
    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]
    source = Image.open(BytesIO(image_bytes)).convert("RGB")
    source = ImageOps.exif_transpose(source)

    cards, meta_blocks, footer_blocks = _extract_blocks(spec)
    issues: list[str] = []
    issues.extend(validate_blueprint(spec, cards, footer_blocks))
    issues.extend(atomic_blueprint_issues(spec))
    issues.extend(validate_contract_on_spec(spec))

    crop_reports: list[dict[str, Any]] = []
    for idx, block in enumerate(cards, start=1):
        policy = str(block.get("source_policy") or "generate_new").lower()
        title = str(block.get("title") or block.get("id") or f"Блок {idx}")
        crop_ok = True
        crop_issue = ""
        crop_size = None
        if policy in {"preserve_from_reference", "use_reference_and_clean"}:
            crop = _crop_from_source(source, block)
            q_issues = _crop_quality_issues(crop, block)
            if crop is None or q_issues:
                crop_ok = False
                crop_issue = f"card_{idx}_crop_semantic_quality_failed:" + ",".join(q_issues or ["crop_failed_or_not_atomic"])
                issues.append(crop_issue)
            else:
                crop_size = f"{crop.width}x{crop.height}"
        crop_reports.append({
            "idx": idx,
            "id": block.get("id"),
            "title": title,
            "type": block.get("type"),
            "source_policy": policy,
            "source_bbox": block.get("source_bbox"),
            "crop_ok": crop_ok,
            "crop_size": crop_size,
            "issue": crop_issue,
        })

    format_profile = choose_format(spec, len(cards), len(footer_blocks))
    W = int(format_profile["w"])
    H = int(format_profile["h"])
    ai_ok, ai_layouts, ai_layout_issues = validate_ai_layout(cards, W, H)
    issues.extend(ai_layout_issues)
    plan = auto_plan_layout(W, H, cards, footer_blocks)
    issues.extend(final_qa(plan, cards, footer_blocks))

    critical = _critical_issues(issues)
    return {
        "ok": not critical,
        "critical_issues": critical,
        "issues": issues,
        "cards": crop_reports,
        "footer_blocks": [str(x.get("title") or x.get("type") or "footer") for x in footer_blocks],
        "format": format_profile,
        "plan_canvas": plan.get("canvas"),
        "source_image_size": f"{source.width}x{source.height}",
    }


def generate_component_crop_preview_image(
    reconstruction: ContentReconstruction,
    asset: ContentAsset | None,
) -> tuple[str, str, list[str]]:
    """Create a Telegram-friendly debug preview of extracted atomic blocks.

    The preview lets the user see whether source_bbox actually points to the
    correct fragments before final infographic generation.
    """
    if not has_component_reference(asset):
        raise ComponentInfographicError("У исходника нет изображения для preview crop-блоков.")

    spec = normalize_atomic_blocks(_load_spec(reconstruction))
    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]
    source = Image.open(BytesIO(image_bytes)).convert("RGB")
    source = ImageOps.exif_transpose(source)
    cards, meta_blocks, footer_blocks = _extract_blocks(spec)
    report = analyze_component_infographic_blueprint(reconstruction, asset)

    W = 1200
    cols = 3 if len(cards) >= 5 else 2
    tile_w = (W - 40 - (cols - 1) * 18) // cols
    tile_h = 250
    header_h = 160
    rows = max(1, math.ceil(max(1, len(cards)) / cols))
    H = header_h + rows * (tile_h + 18) + 190
    canvas = Image.new("RGB", (W, H), "#F7FBFA")
    draw = ImageDraw.Draw(canvas)
    title_font = _find_font(40, bold=True)
    text_font = _find_font(23, bold=False)
    small_font = _find_font(19, bold=False)
    draw.text((28, 24), "Проверка crop-блоков перед сборкой", font=title_font, fill="#12304A")
    status = "OK: можно собирать" if report["ok"] else "Нужна корректировка blueprint"
    status_color = "#1D918E" if report["ok"] else "#D33A2C"
    draw.text((28, 78), status, font=text_font, fill=status_color)
    draw.text((28, 112), f"Карточек: {len(cards)} | Footer: {len(footer_blocks)} | Source: {source.width}x{source.height}", font=small_font, fill="#536A75")

    x0 = 20
    y0 = header_h
    for idx, block in enumerate(cards, start=1):
        row = (idx - 1) // cols
        col = (idx - 1) % cols
        x = x0 + col * (tile_w + 18)
        y = y0 + row * (tile_h + 18)
        _draw_round_rect(draw, (x, y, x + tile_w, y + tile_h), radius=18, fill="#FFFFFF", outline="#B8D6D9", width=2)
        title = str(block.get("title") or block.get("id") or f"Блок {idx}")
        policy = str(block.get("source_policy") or "generate_new")
        draw.text((x + 16, y + 12), f"{idx}. {title[:28]}", font=_find_font(24, bold=True), fill="#12304A")
        draw.text((x + 16, y + 42), policy[:38], font=small_font, fill="#536A75")
        crop = None
        if policy.lower() in {"preserve_from_reference", "use_reference_and_clean"}:
            crop = _crop_from_source(source, block)
        if crop is not None:
            crop.thumbnail((tile_w - 36, 150), Image.LANCZOS)
            canvas.paste(crop, (x + (tile_w - crop.width)//2, y + 76))
            draw.text((x + 16, y + tile_h - 32), "crop OK", font=small_font, fill="#1D918E")
        elif policy.lower() in {"replace_with_new", "generate_new"}:
            draw.text((x + 16, y + 105), "будет сгенерирован новый элемент", font=small_font, fill="#2D83C5")
        else:
            draw.text((x + 16, y + 105), "crop НЕ найден", font=small_font, fill="#D33A2C")

    footer_y = header_h + rows * (tile_h + 18) + 10
    issues = report.get("critical_issues") or []
    if issues:
        draw.text((28, footer_y), "Критические проблемы:", font=_find_font(26, bold=True), fill="#D33A2C")
        yy = footer_y + 36
        for issue in issues[:5]:
            yy = _draw_text_block(draw, f"• {issue}", (42, yy), small_font, "#12304A", W - 80, max_lines=1)
    else:
        draw.text((28, footer_y), "Критических проблем не найдено. Проверьте визуально, что блоки соответствуют исходнику.", font=text_font, fill="#1D918E")

    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"crop-preview-reconstruction-{reconstruction.id}.png"
    path = images_dir / filename
    canvas.save(path, format="PNG", optimize=True)

    text_report = "\n".join([
        "Crop-preview перед финальной сборкой",
        f"source_image_size={report.get('source_image_size')}",
        f"cards={len(cards)} footer_blocks={len(footer_blocks)}",
        f"critical_issues={'; '.join(report.get('critical_issues') or []) or 'none'}",
        "Если crop-блоки неверные, нужно заново выполнить реконструкцию или уточнить промпт/blueprint.",
    ])
    return str(path), text_report, list(report.get("critical_issues") or [])



def _fit_image_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Fit visual into target area with cover mode, avoiding tiny thumbnails."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width <= 0 or img.height <= 0:
        return Image.new("RGB", (target_w, target_h), "#FFFFFF")
    scale = max(target_w / img.width, target_h / img.height)
    nw = max(1, int(img.width * scale))
    nh = max(1, int(img.height * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    x = max(0, (nw - target_w) // 2)
    y = max(0, (nh - target_h) // 2)
    return resized.crop((x, y, x + target_w, y + target_h))


def _render_technical_draft(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset,
    spec: dict[str, Any],
    source: Image.Image,
    cards: list[dict[str, Any]],
    footer_blocks: list[dict[str, Any]],
    plan: dict[str, Any],
    style: str,
) -> tuple[str, str, list[str]]:
    """Create a strict technical draft: all blocks present, no design fantasy.

    This draft is intentionally boring, but it must be accurate. It becomes the
    reference for the AI design-polish stage.
    """
    issues: list[str] = []
    W = int((plan.get("canvas") or {}).get("w", 1200))
    H = int((plan.get("canvas") or {}).get("h", 1500))
    canvas = Image.new("RGB", (W, H), "#F7FBFA")
    draw = ImageDraw.Draw(canvas)
    accent = "#1D918E"
    dark = "#12304A"

    headline = post.headline or (spec.get("title") or {}).get("final") or reconstruction.final_title or post.title
    subtitle = (spec.get("structure") or {}).get("subtitle") or "Кратко, наглядно и безопасно"
    disclaimer = (spec.get("medical_audit") or {}).get("disclaimer") or "Реакции могут отличаться. Точный диагноз ставит врач."

    header = plan.get("header") or {"x": 34, "y": 24, "w": W - 68, "h": 190}
    hx, hy, hw, hh = int(header["x"]), int(header["y"]), int(header["w"]), int(header["h"])
    disclaimer_w = 320 if W >= 1100 else 270
    title_max_w = max(520, hw - disclaimer_w - 110)
    draw.text((hx + 5, hy + 8), "✚", font=_find_font(48, bold=True), fill=accent)
    _draw_text_block(draw, str(headline), (hx + 72, hy + 4), _find_font(50, bold=True), dark, title_max_w, line_gap=4, max_lines=2)
    _draw_text_block(draw, str(subtitle), (hx + 72, hy + 108), _find_font(30, bold=True), accent, title_max_w, line_gap=3, max_lines=1)
    _draw_round_rect(draw, (W - disclaimer_w - 30, hy + 2, W - 30, hy + 128), radius=18, fill="#FFFFFF", outline="#C7DEE0", width=2)
    _draw_text_block(draw, str(disclaimer), (W - disclaimer_w - 10, hy + 18), _find_font(22, bold=False), dark, disclaimer_w - 42, line_gap=4, max_lines=4)

    card_layouts = plan.get("cards") or []
    for idx, block in enumerate(cards):
        if idx >= len(card_layouts):
            issues.append(f"card_{idx+1}_has_no_layout")
            continue
        l = card_layouts[idx]
        x, y, w, h = int(l["x"]), int(l["y"]), int(l["w"]), int(l["h"])
        _draw_round_rect(draw, (x, y, x + w, y + h), radius=20, fill="#FFFFFF", outline="#B8D6D9", width=2)
        num = str(block.get("number") or idx + 1)
        draw.ellipse((x + 16, y + 16, x + 56, y + 56), fill=accent)
        draw.text((x + 28, y + 22), num, font=_find_font(22, bold=True), fill="#FFFFFF")
        _draw_text_block(draw, str(block.get("title") or f"Блок {idx+1}"), (x + 68, y + 16), _find_font(30, bold=True), dark, w - 86, max_lines=1)

        # Larger visual zone than v28: technical draft must be visually useful.
        visual_top = y + 64
        visual_h = max(110, int(h * 0.50))
        visual_w = max(120, int(w * 0.78))
        visual_x = x + (w - visual_w) // 2
        visual_y = visual_top
        visual = _prepare_visual(source, block, style, target_w=visual_w, target_h=visual_h)
        # v34: do not cover-crop prepared visual components. The source crop has
        # already been semantically trimmed; cover mode can cut off insects/lesions.
        canvas.paste(visual, (visual_x, visual_y))

        lines = block.get("lines") or []
        if isinstance(lines, str):
            lines = [lines]
        yy = visual_y + visual_h + 12
        tf = _find_font(21, bold=False)
        for line in lines[:2]:
            line = str(line).strip()
            if not line:
                continue
            draw.text((x + 22, yy), "•", font=tf, fill=accent)
            yy = _draw_text_block(draw, line, (x + 46, yy), tf, dark, w - 62, line_gap=4, max_lines=2)

    footer_layouts = plan.get("footer") or []
    for idx, fb in enumerate(footer_blocks[:len(footer_layouts)]):
        l = footer_layouts[idx]
        title = str(fb.get("title") or "Важно")
        items = _as_list(fb.get("lines"))[:5]
        t = str(fb.get("type") or "").lower()
        color = "#E8483C" if "warning" in t or "срочно" in title.lower() else ("#2D83C5" if "footer" in t or "проф" in title.lower() else accent)
        icon = "!" if color == "#E8483C" else ("i" if color == "#2D83C5" else "✓")
        _info_box(canvas, draw, (int(l["x"]), int(l["y"]), int(l["x"] + l["w"]), int(l["y"] + l["h"])), title, items, color, icon=icon)

    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"v29-technical-draft-{reconstruction.id}-post-{post.id}-{_slugify(post.title)}.png"
    path = images_dir / filename
    canvas.save(path, format="PNG", optimize=True)
    return str(path), "technical_draft", issues



def _image_data_url(path: str) -> str:
    """Convert local PNG/JPEG image to a data URL for vision QA."""
    suffix = Path(path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(Path(path).read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"


def _safe_json_from_text(text: str) -> dict[str, Any]:
    try:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)
        return json.loads(cleaned)
    except Exception:
        return {"recommendation": "use_draft", "issues": ["qa_json_parse_failed"]}


def _qa_polished_image_with_ai(
    draft_path: str,
    polished_path: str,
    spec: dict[str, Any],
    expected_cards: int,
    expected_footer_blocks: int,
) -> dict[str, Any]:
    """Final QA gate for AI-polished infographic.

    The draft is the accuracy reference. The polished version may be prettier, but
    it is only accepted if it keeps all important blocks, readable Russian text,
    and no visible cropping. On any tool/API/parse error we prefer the draft.
    """
    try:
        client = _get_client()
        prompt = f"""
Ты — строгий QA-редактор медицинской инфографики.

Сравни две картинки:
1) technical draft — точный черновик. Его можно считать эталоном структуры.
2) polished image — дизайн-версия после перерисовки.

Твоя задача — решить, можно ли использовать polished image вместо technical draft.

Проверь строго:
- сохранились ли все основные карточки/блоки;
- не исчезли ли элементы;
- не обрезаны ли верх, низ или текстовые блоки;
- русский текст читаемый, без мусорных символов и странных слов;
- количество карточек примерно соответствует черновику;
- нижние блоки warning/action/prevention не потеряны;
- нет водяных знаков, username и чужого бренда;
- медицинский смысл не искажен;
- выполнены ли required_elements из reconstruction_contract;
- отсутствуют ли forbidden_elements из reconstruction_contract;
- выполнены ли replacement_rules: удаленный элемент не должен быть виден или написан, новый элемент должен быть виден или написан;
- polished image не должен быть красивее ценой потери структуры.

Ожидаемо: карточек = {expected_cards}, нижних/служебных блоков = {expected_footer_blocks}.

Structured reconstruction contract:
{contract_summary(spec)}

Если есть сомнения — выбирай technical draft, а не polished image.

Верни строго JSON:
{{
  "recommendation": "use_polished" или "use_draft",
  "all_required_blocks_present": true/false,
  "text_readable": true/false,
  "no_cropping": true/false,
  "medical_safety_ok": true/false,
  "issues": ["..."],
  "reason": "короткое объяснение"
}}

Structured spec для проверки смысла:
{json.dumps(spec, ensure_ascii=False)[:10000]}

Reconstruction contract — обязательства, которые должны быть выполнены:
{contract_summary(spec)}
""".strip()
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": "Ты строгий QA-валидатор медицинских инфографик. Отвечай только валидным JSON."},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_text", "text": "TECHNICAL DRAFT:"},
                        {"type": "input_image", "image_url": _image_data_url(draft_path)},
                        {"type": "input_text", "text": "POLISHED IMAGE:"},
                        {"type": "input_image", "image_url": _image_data_url(polished_path)},
                    ],
                },
            ],
        )
        result = _safe_json_from_text(response.output_text)
        rec = str(result.get("recommendation") or "").strip().lower()
        if rec not in {"use_polished", "use_draft"}:
            result["recommendation"] = "use_draft"
            result.setdefault("issues", []).append("qa_invalid_recommendation")
        # v32: any failed QA flag forces technical draft.
        hard_flags = [
            "all_required_blocks_present",
            "text_readable",
            "no_cropping",
            "medical_safety_ok",
            "required_elements_present",
            "forbidden_elements_absent",
            "replacement_rules_ok",
        ]
        for flag in hard_flags:
            if result.get(flag) is False:
                result["recommendation"] = "use_draft"
                result.setdefault("issues", []).append(f"qa_flag_failed:{flag}")
        return result
    except Exception as exc:
        return {
            "recommendation": "use_draft",
            "all_required_blocks_present": False,
            "text_readable": False,
            "no_cropping": False,
            "medical_safety_ok": False,
            "issues": [f"qa_failed: {exc}"],
            "reason": "QA не смог надежно проверить дизайн-версию, поэтому безопаснее использовать technical draft.",
        }

def _polish_design_with_ai(
    draft_path: str,
    reconstruction: ContentReconstruction,
    post: ContentPost,
    spec: dict[str, Any],
    output_w: int,
    output_h: int,
) -> tuple[str, str] | None:
    """Ask image AI to redesign the accurate draft, not invent content.

    If this step fails, the caller should use the technical draft as fallback.
    """
    try:
        client = _get_client()
        title = post.headline or post.title
        prompt = f"""
Перед тобой технический черновик медицинской инфографики. Он содержит правильную структуру, правильное количество блоков, правильные подписи и нужный порядок элементов.

Твоя задача: улучшить ДИЗАЙН, но НЕ менять смысл, количество блоков и подписи.

СТРОГО СОХРАНИ:
- все карточки и все нижние блоки из черновика;
- порядок карточек;
- смысл и названия блоков;
- предупреждения, действия и профилактику;
- русский текст должен остаться читаемым и без ошибок.

УЛУЧШИ:
- композицию;
- расстояния и визуальный баланс;
- современный clean medical design;
- аккуратные скругленные карточки;
- цвета: светлый фон, темно-синий текст, бирюзовые/зеленые медицинские акценты, красный только для warning;
- иконки и визуальные элементы;
- профессиональный вид для Telegram/Instagram клиники.

ЗАПРЕЩЕНО:
- удалять блоки;
- объединять несколько карточек в одну;
- добавлять водяные знаки, username, чужой бренд;
- превращать русский текст в нечитаемые символы;
- обрезать нижние блоки;
- менять медицинские рекомендации на новые факты.

Итоговый заголовок: {title}

Structured spec для понимания смысла:
{json.dumps(spec, ensure_ascii=False)[:12000]}

Reconstruction contract — обязательства, которые нельзя нарушать:
{contract_summary(spec)}
""".strip()
        with open(draft_path, "rb") as image_file:
            size = "1024x1536" if output_h > output_w * 1.15 else "1024x1024"
            response = client.images.edit(
                model=settings.openai_image_model,
                image=image_file,
                prompt=prompt,
                size=size,
                n=1,
            )
        b64_json = getattr(response.data[0], "b64_json", None)
        if not b64_json:
            return None
        images_dir = Path(settings.generated_images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)
        filename = f"v29-polished-infographic-{reconstruction.id}-post-{post.id}-{_slugify(post.title)}.png"
        out = images_dir / filename
        out.write_bytes(base64.b64decode(b64_json))
        return str(out), prompt
    except Exception:
        return None

def generate_crop_assembled_infographic_image(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> tuple[str, str]:
    """v29: Technical Draft Renderer + AI Design Polish.

    The program first builds an accurate technical draft from atomic crops and
    structured footer blocks. Then image AI polishes the design using that draft
    as the reference. If polish fails, the accurate draft is returned.
    """
    if not has_component_reference(asset):
        raise ComponentInfographicError("У исходника нет изображения-референса для crop-and-assemble reconstruction.")

    spec = normalize_atomic_blocks(_load_spec(reconstruction))
    visual_spec = spec.get("visual") or {}
    style = str(visual_spec.get("style") or visual_spec.get("strategy") or "clean medical minimal design")

    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]
    pre_issues = atomic_blueprint_issues(spec)
    pre_issues.extend(validate_contract_on_spec(spec))
    if pre_issues:
        try:
            spec = repair_atomic_blueprint_with_ai(
                client=_get_client(),
                model=settings.openai_model,
                spec=spec,
                image_bytes=image_bytes,
                issues=pre_issues,
                extra_context="Repair before v29 technical draft rendering",
            )
        except Exception:
            pass

    source = Image.open(BytesIO(image_bytes)).convert("RGB")
    source = ImageOps.exif_transpose(source)

    cards, meta_blocks, footer_blocks = _extract_blocks(spec)
    if len(cards) < 2:
        raise ComponentInfographicError("Blueprint не содержит достаточного количества атомарных карточек для crop-and-assemble.")

    validation_issues = validate_blueprint(spec, cards, footer_blocks)
    validation_issues.extend(atomic_blueprint_issues(spec))
    validation_issues.extend(validate_contract_on_spec(spec))

    format_profile = choose_format(spec, len(cards), len(footer_blocks))
    W = int(format_profile["w"])
    H = int(format_profile["h"])

    # v29: for dense infographics always use deterministic safe layout for the draft.
    plan = auto_plan_layout(W, H, cards, footer_blocks)
    if isinstance(plan.get("canvas"), dict):
        W = int(plan["canvas"].get("w", W))
        H = int(plan["canvas"].get("h", H))

    validation_issues.extend(final_qa(plan, cards, footer_blocks))
    critical_validation_issues = _critical_issues(validation_issues) + contract_critical_issues(validation_issues)
    # de-duplicate while preserving order
    critical_validation_issues = list(dict.fromkeys(critical_validation_issues))
    if critical_validation_issues:
        raise ComponentInfographicError(
            "Критические проблемы blueprint/crop перед сборкой:\n" + "\n".join(f"- {x}" for x in critical_validation_issues[:12])
        )

    draft_path, draft_prompt, draft_issues = _render_technical_draft(
        reconstruction=reconstruction,
        post=post,
        asset=asset,  # type: ignore[arg-type]
        spec=spec,
        source=source,
        cards=cards,
        footer_blocks=footer_blocks,
        plan=plan,
        style=style,
    )

    polished = _polish_design_with_ai(
        draft_path=draft_path,
        reconstruction=reconstruction,
        post=post,
        spec=spec,
        output_w=W,
        output_h=H,
    )

    if polished:
        polished_path, polish_prompt = polished
        qa = _qa_polished_image_with_ai(
            draft_path=draft_path,
            polished_path=polished_path,
            spec=spec,
            expected_cards=len(cards),
            expected_footer_blocks=len(footer_blocks),
        )
        use_polished = qa.get("recommendation") == "use_polished"
        final_path = polished_path if use_polished else draft_path
        image_prompt = "\n".join([
            "v30 Draft + Polish + Final QA Pipeline",
            f"technical_draft={draft_path}",
            f"polished_image={polished_path}",
            f"selected_image={final_path}",
            f"final_choice={'polished' if use_polished else 'draft'}",
            f"qa={json.dumps(qa, ensure_ascii=False)[:4000]}",
            f"output_size={W}x{H} aspect={format_profile.get('aspect_ratio')} profile={format_profile.get('name')}",
            f"cards={len(cards)} footer_blocks={len(footer_blocks)}",
            f"validation_issues={'; '.join(validation_issues) if validation_issues else 'none'}",
            f"draft_issues={'; '.join(draft_issues) if draft_issues else 'none'}",
            "AI polish prompt:",
            polish_prompt[:6000],
        ])
        return final_path, image_prompt

    image_prompt = "\n".join([
        "v30 Technical Draft fallback: AI design polish failed or returned no image.",
        f"technical_draft={draft_path}",
        f"selected_image={draft_path}",
        "final_choice=draft",
        f"output_size={W}x{H} aspect={format_profile.get('aspect_ratio')} profile={format_profile.get('name')}",
        f"cards={len(cards)} footer_blocks={len(footer_blocks)}",
        f"validation_issues={'; '.join(validation_issues) if validation_issues else 'none'}",
        f"draft_issues={'; '.join(draft_issues) if draft_issues else 'none'}",
    ])
    return draft_path, image_prompt


# v23 compatibility: keep old function name, but route to v24 engine.
def build_component_infographic_prompt(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> str:
    spec = _load_spec(reconstruction)
    return json.dumps(spec, ensure_ascii=False, indent=2)[:12000]


def generate_component_infographic_image(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> tuple[str, str]:
    return generate_crop_assembled_infographic_image(reconstruction, post, asset)
