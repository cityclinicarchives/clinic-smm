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
from app.services.blueprint_layout import choose_format, auto_plan_layout, validate_ai_layout, validate_blueprint


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
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
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


def _crop_from_source(source: Image.Image, block: dict[str, Any]) -> Image.Image | None:
    bbox = _normalize_bbox(block.get("source_bbox"), source.width, source.height)
    if not bbox:
        bbox = _infer_bbox_from_hint(block.get("source_location_hint"), source.width, source.height)
    if not bbox:
        return None
    crop = source.crop(bbox).convert("RGB")
    # Remove possible rough edges by fitting into square-ish visual zone.
    crop = ImageOps.exif_transpose(crop)
    return crop


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


def _prepare_visual(source: Image.Image, block: dict[str, Any], style: str, target_w: int, target_h: int) -> Image.Image:
    policy = str(block.get("source_policy") or "generate_new").lower()
    visual: Image.Image | None = None
    if policy in {"preserve_from_reference", "use_reference_and_clean"}:
        visual = _crop_from_source(source, block)
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
    head_h = 58
    _draw_round_rect(draw, (x1, y1, x2, y1 + head_h), radius=22, fill=accent, outline=None)
    title_font = _find_font(28, bold=True)
    text_font = _find_font(22, bold=False)
    draw.text((x1 + 24, y1 + 12), icon, font=title_font, fill="#FFFFFF")
    draw.text((x1 + 65, y1 + 12), title[:34], font=title_font, fill="#FFFFFF")
    y = y1 + head_h + 16
    for item in items[:6]:
        draw.text((x1 + 28, y), "•", font=text_font, fill=accent)
        y = _draw_text_block(draw, item, (x1 + 54, y), text_font, "#143047", x2 - x1 - 80, line_gap=6, max_lines=2)


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
    # Add warning/action blocks if they exist separately.
    structure = spec.get("structure") or {}
    if structure.get("warning_block"):
        footer_blocks.append({"type": "warning_block", "title": "Срочно к врачу, если:", "lines": _as_list(structure.get("warning_block"))})
    if structure.get("action_block"):
        footer_blocks.append({"type": "action_block", "title": "Что делать после укуса:", "lines": _as_list(structure.get("action_block"))})
    if structure.get("footer"):
        footer_blocks.append({"type": "footer", "title": "Важно", "lines": _as_list(structure.get("footer"))})
    return content_cards, meta_blocks, footer_blocks


def generate_crop_assembled_infographic_image(
    reconstruction: ContentReconstruction,
    post: ContentPost,
    asset: ContentAsset | None,
) -> tuple[str, str]:
    """v25: Blueprint Layout Validator + Multi-format Layout Planner.

    The reconstruction spec may contain an AI layout, but the program now validates it
    before rendering. If the AI layout is missing/unsafe, we create a deterministic
    multi-format plan that fits all blocks instead of squeezing everything into a
    fixed square grid.
    """
    if not has_component_reference(asset):
        raise ComponentInfographicError("У исходника нет изображения-референса для crop-and-assemble reconstruction.")

    spec = _load_spec(reconstruction)
    visual_spec = spec.get("visual") or {}
    style = str(visual_spec.get("style") or visual_spec.get("strategy") or "clean medical minimal design")

    image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]
    source = Image.open(BytesIO(image_bytes)).convert("RGB")
    source = ImageOps.exif_transpose(source)

    cards, meta_blocks, footer_blocks = _extract_blocks(spec)
    if len(cards) < 2:
        raise ComponentInfographicError("Blueprint не содержит достаточного количества карточек для crop-and-assemble.")

    validation_issues = validate_blueprint(spec, cards, footer_blocks)

    format_profile = choose_format(spec, len(cards), len(footer_blocks))
    W = int(format_profile["w"])
    H = int(format_profile["h"])

    # Try to use AI-provided coordinates only if every content card has a safe layout.
    ai_ok, ai_layouts, ai_layout_issues = validate_ai_layout(cards, W, H)
    if ai_ok:
        layout_mode = "ai_blueprint_layout"
        plan = auto_plan_layout(W, H, [], footer_blocks)
        plan["cards"] = ai_layouts
    else:
        layout_mode = "auto_multiformat_layout"
        validation_issues.extend(ai_layout_issues)
        plan = auto_plan_layout(W, H, cards, footer_blocks)

    headline = post.headline or (spec.get("title") or {}).get("final") or reconstruction.final_title or post.title
    subtitle = (spec.get("structure") or {}).get("subtitle") or "Кратко, наглядно и безопасно"
    disclaimer = (spec.get("medical_audit") or {}).get("disclaimer") or "Реакции могут отличаться. Точный диагноз ставит врач."

    canvas = Image.new("RGB", (W, H), "#F7FBFA")
    draw = ImageDraw.Draw(canvas)
    accent = "#1D918E"
    dark = "#12304A"

    # Header with safe layout zone.
    header = plan.get("header") or {"x": 30, "y": 24, "w": W - 60, "h": 170}
    hx, hy, hw, hh = header["x"], header["y"], header["w"], header["h"]
    title_font = _find_font(62 if len(str(headline)) < 44 else 52, bold=True)
    subtitle_font = _find_font(36, bold=True)
    text_font = _find_font(23, bold=False)
    draw.text((hx + 18, hy + 12), "✚", font=_find_font(54, bold=True), fill=accent)
    disclaimer_w = 300 if W >= 1150 else 260
    title_max_w = max(460, hw - disclaimer_w - 130)
    y_after = _draw_text_block(draw, str(headline), (hx + 95, hy + 4), title_font, dark, title_max_w, line_gap=5, max_lines=2)
    _draw_text_block(draw, str(subtitle)[:90], (hx + 95, max(hy + 92, y_after + 2)), subtitle_font, accent, title_max_w, line_gap=3, max_lines=1)
    _draw_round_rect(draw, (W - disclaimer_w - 30, hy + 4, W - 30, hy + min(128, hh - 4)), radius=20, fill="#FFFFFF", outline="#C7DEE0", width=2)
    _draw_text_block(draw, str(disclaimer), (W - disclaimer_w - 8, hy + 22), text_font, dark, disclaimer_w - 44, line_gap=5, max_lines=4)

    # Cards: render every card that exists in blueprint. No silent dropping.
    card_layouts = plan.get("cards") or []
    for idx, block in enumerate(cards):
        if idx >= len(card_layouts):
            validation_issues.append(f"card_{idx+1}_has_no_layout_and_was_not_rendered")
            continue
        l = card_layouts[idx]
        x1, y1, x2, y2 = l["x"], l["y"], l["x"] + l["w"], l["y"] + l["h"]
        visual = _prepare_visual(source, block, style, target_w=int(l["w"] * 0.78), target_h=int(l["h"] * 0.42))
        _card(canvas, draw, (x1, y1, x2, y2), block, visual=visual, accent=accent)

    # Footer/meta blocks: warning/actions/prevention all rendered if there is room.
    footer_layouts = plan.get("footer") or []
    for idx, fb in enumerate(footer_blocks[:len(footer_layouts)]):
        l = footer_layouts[idx]
        title = str(fb.get("title") or "Важно")
        items = _as_list(fb.get("lines"))
        t = str(fb.get("type") or "").lower()
        color = "#E8483C" if "warning" in t or "срочно" in title.lower() else ("#2D83C5" if "footer" in t or "проф" in title.lower() else accent)
        icon = "!" if color == "#E8483C" else ("i" if color == "#2D83C5" else "✓")
        _info_box(canvas, draw, (l["x"], l["y"], l["x"] + l["w"], l["y"] + l["h"]), title, items, color, icon=icon)

    # If validation found issues, put a small non-public marker only in prompt report, not on image.
    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"v25-blueprint-infographic-{reconstruction.id}-post-{post.id}-{_slugify(post.title)}.png"
    path = images_dir / filename
    canvas.save(path, format="PNG", optimize=True)

    prompt_report = "\n".join([
        "v25 Blueprint Layout Validator + Multi-format Layout Planner",
        f"source_image_size={source.width}x{source.height}",
        f"output_size={W}x{H} aspect={format_profile.get('aspect_ratio')} profile={format_profile.get('name')}",
        f"cards={len(cards)} footer_blocks={len(footer_blocks)} layout_mode={layout_mode}",
        f"validation_issues={'; '.join(validation_issues) if validation_issues else 'none'}",
        "Правило v25: все blueprint-карточки должны быть отрендерены; если AI layout небезопасен, включается auto layout.",
    ])
    return str(path), prompt_report


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
