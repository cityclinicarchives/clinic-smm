from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.config import settings


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
    return f"""
Создай один чистый смысловой PNG-объект для медицинской SMM-инфографики.

Тема инфографики: {topic}
PNG ID: {task.get('png_id')}
Операция по плану: {task.get('operation')}

Задание:
{instruction}

Обязательно включить: {must_include or 'только смысловой объект по заданию'}.
Обязательно исключить: {must_exclude or 'любой текст, watermark, кнопки интерфейса, старый фон'}.

Стиль:
- clean medical illustration;
- мягкие градиенты;
- спокойный медицинский SMM-дизайн;
- без крови, некроза, пугающих деталей;
- без текста внутри изображения;
- объект должен хорошо смотреться на светлой карточке инфографики.
""".strip()


def generate_semantic_pngs(asset_id: int, limit: int | None = None) -> tuple[list[str], list[str]]:
    data = load_analysis(asset_id)
    payload = _payload(data)
    tasks = payload.get("semantic_png_plan") or []
    if not isinstance(tasks, list) or not tasks:
        raise SemanticAssetError("В JSON анализа нет semantic_png_plan.")

    out_dir = semantic_png_dir(asset_id, _state_id(data))
    done: list[str] = []
    skipped: list[str] = []
    client = _client()

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
        prompt = build_semantic_png_prompt(task, payload)
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
            count += 1
        except Exception as exc:
            raise SemanticAssetError(f"Ошибка генерации {png_id}: {exc}") from exc

    manifest = {
        "asset_id": asset_id,
        "project_state_id": _state_id(data),
        "analysis_path": data.get("_analysis_path"),
        "generated": done,
        "skipped_existing": skipped,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return done, skipped


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
    cards = cards[:9]
    pngs = _png_lookup(asset_id, state_id, payload)

    margin_x = 50
    gap = 24
    card_w = (width - margin_x * 2 - gap * 2) // 3
    card_h = 275
    start_y = 220

    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        col = idx % 3
        row = idx // 3
        x = margin_x + col * (card_w + gap)
        cy = start_y + row * (card_h + 22)
        draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=28, fill="#FFFFFF", outline="#F7D8C8", width=2)
        png_id = str(card.get("png_id") or "")
        png_path = pngs.get(png_id)
        if png_path and png_path.exists():
            try:
                p = Image.open(png_path).convert("RGBA")
                p.thumbnail((150, 150))
                img.paste(p, (x + (card_w - p.width) // 2, cy + 15), p if p.mode == "RGBA" else None)
            except Exception:
                pass
        else:
            draw.ellipse((x + card_w//2 - 58, cy + 25, x + card_w//2 + 58, cy + 141), fill="#F7D8C8", outline="#F2B8A8")

        title = str(card.get("title") or card.get("card_id") or "")
        ty = cy + 168
        for line in _wrap(draw, title, card_title_font, card_w - 28)[:2]:
            tw = draw.textbbox((0, 0), line, font=card_title_font)[2]
            draw.text((x + (card_w - tw) // 2, ty), line, fill="#1E1E1E", font=card_title_font)
            ty += 32
        text = str(card.get("short_text") or "")
        for line in _wrap(draw, text, card_text_font, card_w - 36)[:3]:
            tw = draw.textbbox((0, 0), line, font=card_text_font)[2]
            draw.text((x + (card_w - tw) // 2, ty + 4), line, fill="#444444", font=card_text_font)
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
