from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


COMMON_FONT_CANDIDATES = [
    # Railway / Debian / Ubuntu common fonts
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _matplotlib_dejavu_font() -> str | None:
    """
    Matplotlib поставляется с DejaVu Sans.
    Этот шрифт нормально поддерживает кириллицу, поэтому используем его как надежный fallback.
    """
    try:
        import matplotlib

        font_path = (
            Path(matplotlib.get_data_path())
            / "fonts"
            / "ttf"
            / "DejaVuSans-Bold.ttf"
        )
        if font_path.exists():
            return str(font_path)
    except Exception:
        return None
    return None


def _find_font_path() -> str:
    candidates = list(COMMON_FONT_CANDIDATES)
    mpl_font = _matplotlib_dejavu_font()
    if mpl_font:
        candidates.insert(0, mpl_font)

    for font_path in candidates:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "Не найден TrueType-шрифт с поддержкой кириллицы. "
        "Проверьте, что в requirements.txt есть matplotlib."
    )


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_find_font_path(), size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    words = text.strip().split()
    if not words:
        return []

    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = word
        else:
            # Очень длинное слово: оставляем как есть, но не уменьшаем весь текст до нечитаемого размера.
            lines.append(word)
            current = ""

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    return lines


def _fit_font_and_lines(
    headline: str,
    image_width: int,
    image_height: int,
    max_lines: int = 2,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """
    Подбирает крупный шрифт и переносы.
    Важно: не уходим в слишком маленький кегль, иначе заголовок выглядит как нечитаемый штрихкод.
    """
    max_text_width = int(image_width * 0.86)
    min_size = max(42, image_width // 24)
    max_size = max(58, image_width // 15)

    for size in range(max_size, min_size - 1, -2):
        font = _load_font(size)
        lines = _wrap_text(headline, font, max_text_width, max_lines=max_lines)
        if not lines:
            continue

        dummy = Image.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy)
        line_gap = int(size * 0.26)
        text_height = sum(_text_height(draw, line, font) for line in lines)
        total_height = text_height + line_gap * (len(lines) - 1)
        max_panel_height = int(image_height * 0.24)

        if total_height <= max_panel_height:
            return font, lines

    font = _load_font(min_size)
    return font, _wrap_text(headline, font, max_text_width, max_lines=max_lines)


def add_headline_to_image(image_path: str, headline: str) -> str:
    """
    Создает копию изображения с крупным читабельным заголовком внизу.
    Возвращает путь к новому файлу.
    """
    source_path = Path(image_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Изображение не найдено: {image_path}")

    headline = (headline or "").strip()
    if not headline:
        return image_path

    image = Image.open(source_path).convert("RGBA")
    width, height = image.size

    font, lines = _fit_font_and_lines(headline, width, height, max_lines=2)
    if not lines:
        return image_path

    draw_tmp = ImageDraw.Draw(image)
    font_size = getattr(font, "size", max(48, width // 20))
    line_gap = int(font_size * 0.26)
    line_heights = [_text_height(draw_tmp, line, font) for line in lines]

    padding_y = int(height * 0.045)
    text_block_height = sum(line_heights) + line_gap * (len(lines) - 1)
    panel_height = max(int(height * 0.14), text_block_height + padding_y * 2)
    panel_top = height - panel_height

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))

    # Светлая полупрозрачная плашка: хорошо читается и не выглядит как Telegram caption.
    panel = Image.new("RGBA", (width, panel_height), (255, 255, 255, 235))
    overlay.paste(panel, (0, panel_top))

    # Мягкая тень сверху плашки.
    shadow = Image.new("RGBA", (width, 10), (0, 0, 0, 42))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    overlay.paste(shadow, (0, max(0, panel_top - 5)))

    composed = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(composed)

    y = panel_top + (panel_height - text_block_height) // 2
    for line, line_height in zip(lines, line_heights):
        text_width = _text_width(draw, line, font)
        x = (width - text_width) // 2

        # Небольшая светлая подложка + темный текст повышают читаемость после сжатия Telegram.
        draw.text((x + 1, y + 1), line, font=font, fill=(255, 255, 255, 180))
        draw.text((x, y), line, font=font, fill=(16, 24, 39, 255))
        y += line_height + line_gap

    output_path = source_path.with_name(f"{source_path.stem}-headline{source_path.suffix}")
    composed.convert("RGB").save(output_path, quality=95)
    return str(output_path)
