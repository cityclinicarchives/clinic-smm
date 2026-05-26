import json
import math
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.models import ContentReconstruction


class InfographicRenderError(RuntimeError):
    pass


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
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
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int | None = None) -> list[str]:
    words = str(text or "").replace("\n", " ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > 3:
            lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return lines


def _parse_spec(reconstruction: ContentReconstruction) -> dict[str, Any]:
    raw = getattr(reconstruction, "reconstruction_spec", None) or ""
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass

    # Fallback для старых реконструкций v20.
    blocks = []
    structure = reconstruction.infographic_structure or ""
    for line in structure.splitlines():
        line = line.strip(" -•\t")
        if not line:
            continue
        if len(blocks) < 10:
            blocks.append({"title": line[:70], "lines": []})

    return {
        "content_type": reconstruction.content_type or "infographic",
        "title": {"final": reconstruction.final_title or "Полезная памятка"},
        "structure": {
            "kind": "infographic",
            "subtitle": "Коротко и понятно",
            "blocks": blocks or [{"title": reconstruction.final_title or "Важно знать", "lines": [reconstruction.additions or reconstruction.medical_audit or "Обсудите симптомы с врачом."]}],
            "footer": "Информация носит справочный характер и не заменяет консультацию врача.",
        },
        "visual": {"style": reconstruction.visual_strategy or "clean medical infographic"},
    }


def render_infographic_from_reconstruction(reconstruction: ContentReconstruction) -> tuple[str, str]:
    """Детерминированно собирает инфографику через Pillow.

    Главная цель v21: не отдавать русские подписи и медицинскую структуру image-модели,
    а самим контролировать текст, орфографию и блоки.
    """
    spec = _parse_spec(reconstruction)
    title = ((spec.get("title") or {}).get("final") or reconstruction.final_title or "Полезная памятка").strip()
    structure = spec.get("structure") or {}
    subtitle = (structure.get("subtitle") or "что важно знать").strip()
    blocks = structure.get("blocks") or []
    footer = (structure.get("footer") or "Информация носит справочный характер и не заменяет консультацию врача.").strip()

    if not isinstance(blocks, list):
        blocks = []
    blocks = [b for b in blocks if isinstance(b, dict)]
    if not blocks:
        blocks = [{"title": "Что важно", "lines": [reconstruction.medical_audit or "Обсудите симптомы с врачом."]}]

    # Не пытаемся втиснуть слишком много: лучше аккуратная памятка, чем нечитаемая простыня.
    blocks = blocks[:12]

    W = H = 1024
    img = Image.new("RGB", (W, H), "#F7FAFC")
    draw = ImageDraw.Draw(img)

    navy = "#14324A"
    blue = "#1E5F8C"
    teal = "#2D9C8C"
    orange = "#E76F36"
    gray = "#425466"
    light_blue = "#E8F3F7"
    light_green = "#EAF6EF"
    light_orange = "#FFF2E8"
    card_border = "#D6E2EA"
    colors = [light_blue, light_green, light_orange, "#F1EDF9", "#EEF7FB", "#FFF7DD"]
    accent_colors = [blue, teal, orange, "#6B5BAA", "#3B82A0", "#B7791F"]

    # Header
    y = 32
    title_font = _font(48, bold=True)
    if len(title) > 58:
        title_font = _font(42, bold=True)
    if len(title) > 82:
        title_font = _font(36, bold=True)
    title_lines = _wrap_text(draw, title, title_font, W - 96, max_lines=3)
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        draw.text(((W - (bbox[2] - bbox[0])) / 2, y), line, fill=navy, font=title_font)
        y += (bbox[3] - bbox[1]) + 8

    sub_font = _font(24, bold=True)
    sub_text = subtitle[:120]
    sub_lines = _wrap_text(draw, sub_text, sub_font, W - 160, max_lines=2)
    sub_h = len(sub_lines) * 32 + 20
    draw.rounded_rectangle((70, y + 8, W - 70, y + 8 + sub_h), radius=22, fill=navy)
    sy = y + 18
    for line in sub_lines:
        bbox = draw.textbbox((0, 0), line, font=sub_font)
        draw.text(((W - (bbox[2] - bbox[0])) / 2, sy), line, fill="white", font=sub_font)
        sy += 32
    y += sub_h + 36

    n = len(blocks)
    cols = 2 if n <= 8 else 3
    rows = math.ceil(n / cols)
    gap = 14
    margin = 32
    footer_h = 92
    available_h = H - y - footer_h - 28
    card_w = (W - margin * 2 - gap * (cols - 1)) // cols
    card_h = max(118, available_h // rows - gap)

    title_card_font = _font(22 if cols == 3 else 26, bold=True)
    body_font = _font(17 if cols == 3 else 19, bold=False)
    small_font = _font(15 if cols == 3 else 17, bold=False)
    num_font = _font(24 if cols == 3 else 28, bold=True)

    for idx, block in enumerate(blocks):
        row = idx // cols
        col = idx % cols
        x = margin + col * (card_w + gap)
        cy = y + row * (card_h + gap)
        fill = colors[idx % len(colors)]
        accent = accent_colors[idx % len(accent_colors)]
        draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=20, fill=fill, outline=card_border, width=2)
        # number badge
        draw.ellipse((x + 14, cy + 14, x + 50, cy + 50), fill=accent)
        num = str(idx + 1)
        nb = draw.textbbox((0, 0), num, font=num_font)
        draw.text((x + 32 - (nb[2]-nb[0])/2, cy + 32 - (nb[3]-nb[1])/2 - 2), num, fill="white", font=num_font)

        tx = x + 60
        ty = cy + 14
        bw = card_w - 74
        heading = str(block.get("title") or block.get("heading") or "Важно").strip()
        h_lines = _wrap_text(draw, heading, title_card_font, bw, max_lines=2)
        for line in h_lines:
            draw.text((tx, ty), line, fill=navy, font=title_card_font)
            ty += 28 if cols == 3 else 32

        ty += 4
        lines = block.get("lines") or block.get("items") or []
        if isinstance(lines, str):
            lines = [lines]
        if not isinstance(lines, list):
            lines = []
        max_items = 3 if card_h < 145 else 4
        for item in lines[:max_items]:
            item_text = str(item).strip()
            if not item_text:
                continue
            wrapped = _wrap_text(draw, "• " + item_text, body_font, card_w - 34, max_lines=2)
            for wl in wrapped:
                draw.text((x + 18, ty), wl, fill=gray, font=body_font)
                ty += 22 if cols == 3 else 25
            ty += 2

        note = str(block.get("note") or "").strip()
        if note and ty < cy + card_h - 28:
            draw.text((x + 18, cy + card_h - 28), note[:44], fill=accent, font=small_font)

    # Footer / CTA
    fy = H - footer_h - 18
    draw.rounded_rectangle((margin, fy, W - margin, H - 24), radius=22, fill="#EAF6EF", outline="#C8DFD4", width=2)
    footer_font = _font(22, bold=True)
    body_footer_font = _font(18)
    draw.text((margin + 24, fy + 16), "Важно", fill=teal, font=footer_font)
    footer_lines = _wrap_text(draw, footer, body_footer_font, W - margin * 2 - 150, max_lines=2)
    fty = fy + 48
    for line in footer_lines:
        draw.text((margin + 24, fty), line, fill=gray, font=body_footer_font)
        fty += 24

    images_dir = Path(settings.generated_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    path = images_dir / f"reconstruction-{reconstruction.id}-infographic.png"
    img.save(path, "PNG")

    prompt_info = "deterministic_pillow_infographic_renderer_v21: текст и структура взяты из reconstruction_spec, не из image AI"
    return str(path), prompt_info
