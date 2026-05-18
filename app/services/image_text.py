from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont, ImageFilter


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.strip().split()
    if not words:
        return []

    lines: list[str] = []
    current = ""
    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)

    for word in words:
        candidate = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:3]


def add_headline_to_image(image_path: str, headline: str) -> str:
    """
    Создает копию изображения с заголовком внизу.
    Возвращает путь к новому файлу.
    """
    source_path = Path(image_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Изображение не найдено: {image_path}")

    image = Image.open(source_path).convert("RGBA")
    width, height = image.size

    # Подбираем размер шрифта под ширину картинки.
    font_size = max(38, width // 22)
    font = _load_font(font_size)
    max_text_width = int(width * 0.86)
    lines = _wrap_text(headline, font, max_text_width)

    if not lines:
        return image_path

    draw_tmp = ImageDraw.Draw(image)
    line_heights = []
    for line in lines:
        bbox = draw_tmp.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])

    padding_x = int(width * 0.07)
    padding_y = int(height * 0.045)
    line_gap = int(font_size * 0.32)
    text_block_height = sum(line_heights) + line_gap * (len(lines) - 1)
    panel_height = text_block_height + padding_y * 2
    panel_top = height - panel_height

    # Мягко размываем нижнюю часть картинки и кладем полупрозрачную плашку.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    panel = Image.new("RGBA", (width, panel_height), (255, 255, 255, 218))
    overlay.paste(panel, (0, panel_top))

    # Тонкая тень сверху плашки.
    shadow = Image.new("RGBA", (width, 6), (0, 0, 0, 35))
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))
    overlay.paste(shadow, (0, max(0, panel_top - 3)))

    composed = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(composed)

    y = panel_top + padding_y
    for line, line_height in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        draw.text((x, y), line, font=font, fill=(22, 28, 38, 255))
        y += line_height + line_gap

    output_path = source_path.with_name(f"{source_path.stem}-headline{source_path.suffix}")
    composed.convert("RGB").save(output_path, quality=95)
    return str(output_path)
